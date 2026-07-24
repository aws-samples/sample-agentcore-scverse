/**
 * ScverseComputeBackend Construct
 *
 * Provisions the dual-compute stack that sits behind the scverse Gateway:
 *   - DynamoDB Job Table (scverse-job-table) with TTL + PITR
 *   - SNS Notification Topic (scverse-job-notifications)
 *   - Container Lambda (scanpy QC/preprocessing) — sync path
 *   - ECS Fargate cluster + task definition (full scverse stack) — async path
 *   - Step Functions state machine (multi-step pipeline orchestration)
 *   - VPC Gateway Endpoint for S3 (cost + latency)
 *
 * Wires IAM so the Gateway Lambda can invoke Compute Lambda, run ECS tasks,
 * start Step Functions executions, read/write the Job Table, publish to SNS,
 * and list datasets in S3. Also grants Fargate task role read/write on its
 * own job prefix and read on dataset files.
 */

import * as cdk from "aws-cdk-lib";
import { Duration } from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecrAssets from "aws-cdk-lib/aws-ecr-assets";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as sfn from "aws-cdk-lib/aws-stepfunctions";
import * as sns from "aws-cdk-lib/aws-sns";
import { NagSuppressions } from "cdk-nag";
import { Construct } from "constructs";
import * as fs from "fs";
import * as path from "path";

export interface ScverseComputeBackendProps {
    vpc: ec2.IVpc;
    securityGroup: ec2.ISecurityGroup;
    storageBucket: s3.IBucket;
    gatewayLambda: lambda.Function;
    logsKey: kms.IKey;
}

export class ScverseComputeBackend extends Construct {
    public readonly jobTable: dynamodb.Table;
    public readonly notificationTopic: sns.Topic;
    public readonly computeLambda: lambda.DockerImageFunction;
    public readonly ecsCluster: ecs.Cluster;
    public readonly fargateTaskDefinition: ecs.FargateTaskDefinition;
    public readonly pipelineStateMachine: sfn.StateMachine;

    constructor(scope: Construct, id: string, props: ScverseComputeBackendProps) {
        super(scope, id);

        // --- DynamoDB Job Table ----------------------------------------------
        // Customer-managed KMS key so job records are encrypted with a key the
        // customer controls, rotates, and audits (CloudTrail key-usage events).
        const tableKey = new kms.Key(this, "JobTableKey", {
            description: "Encrypts the scverse job table",
            enableKeyRotation: true,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
        });
        this.jobTable = new dynamodb.Table(this, "JobTable", {
            tableName: "scverse-job-table",
            partitionKey: { name: "job_id", type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
            timeToLiveAttribute: "ttl",
            pointInTimeRecovery: true,
            encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryptionKey: tableKey,
        });

        // --- SNS Notification Topic ------------------------------------------
        // Customer-managed KMS key with rotation so notification messages are
        // encrypted at rest — the secure default to demonstrate in copied code.
        const notificationKey = new kms.Key(this, "JobNotificationKey", {
            description: "Encrypts scverse job notification messages",
            enableKeyRotation: true,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
        });
        this.notificationTopic = new sns.Topic(this, "JobNotificationTopic", {
            topicName: "scverse-job-notifications",
            displayName: "scverse Job Notifications",
            masterKey: notificationKey,
        });

        // --- ECR Repositories ------------------------------------------------
        // Declared explicitly so repositoryName and removal policy are stable,
        // even though CDK will create its own asset repos for the image assets
        // below. These named repos are useful for out-of-band image pushes.
        new ecr.Repository(this, "ComputeLambdaRepo", {
            repositoryName: "scverse-compute-lambda",
            removalPolicy: cdk.RemovalPolicy.DESTROY,
            autoDeleteImages: true,
        });
        new ecr.Repository(this, "FargateWorkerRepo", {
            repositoryName: "scverse-fargate-worker",
            removalPolicy: cdk.RemovalPolicy.DESTROY,
            autoDeleteImages: true,
        });

        // --- Compute Lambda (Container image with scanpy) -------------------
        this.computeLambda = new lambda.DockerImageFunction(this, "ComputeLambda", {
            functionName: "scverse-compute-lambda",
            code: lambda.DockerImageCode.fromImageAsset(
                path.join(__dirname, "../../../../gateways/scverse/compute"),
                {
                    platform: ecrAssets.Platform.LINUX_AMD64,
                    buildArgs: {
                        BUILD_DATE: new Date().toISOString(),
                    },
                }
            ),
            memorySize: 10240,
            ephemeralStorageSize: cdk.Size.gibibytes(10),
            timeout: Duration.seconds(900),
            reservedConcurrentExecutions: 10,
            vpc: props.vpc,
            securityGroups: [props.securityGroup],
            vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
            environment: {
                STORAGE_BUCKET: props.storageBucket.bucketName,
                JOB_TABLE_NAME: "scverse-job-table",
                // Numba and matplotlib need writable cache dirs in Lambda's
                // read-only filesystem. /tmp is the only writable mount.
                NUMBA_CACHE_DIR: "/tmp/numba_cache",
                MPLCONFIGDIR: "/tmp/matplotlib",
                // Disable numba JIT caching to avoid the "no locator" error
                // on Lambda where the filesystem is read-only outside /tmp.
                NUMBA_DISABLE_JIT: "1",
                NUMBA_NUM_THREADS: "1",
            },
            logGroup: new logs.LogGroup(this, "ComputeLambdaLogs", {
                logGroupName: "/aws/lambda/scverse-compute-lambda",
                encryptionKey: props.logsKey,
                retention: logs.RetentionDays.TWO_WEEKS,
                removalPolicy: cdk.RemovalPolicy.DESTROY,
            }),
            description:
                "Container Lambda running scanpy for synchronous QC and preprocessing",
        });

        // --- ECS Cluster -----------------------------------------------------
        this.ecsCluster = new ecs.Cluster(this, "ScverseCluster", {
            clusterName: "scverse-compute-cluster",
            vpc: props.vpc,
            containerInsights: true,
        });

        // --- Fargate Task Definition ----------------------------------------
        this.fargateTaskDefinition = new ecs.FargateTaskDefinition(this, "WorkerTaskDef", {
            family: "scverse-fargate-worker",
            cpu: 4096,
            memoryLimitMiB: 16384,
            runtimePlatform: {
                cpuArchitecture: ecs.CpuArchitecture.X86_64,
                operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
            },
        });
        this.fargateTaskDefinition.addContainer("scverse-worker", {
            image: ecs.ContainerImage.fromAsset(
                path.join(__dirname, "../../../../gateways/scverse/fargate"),
                {
                    platform: ecrAssets.Platform.LINUX_AMD64,
                }
            ),
            logging: ecs.LogDrivers.awsLogs({
                streamPrefix: "scverse-worker",
                logGroup: new logs.LogGroup(this, "FargateWorkerLogs", {
                    encryptionKey: props.logsKey,
                    retention: logs.RetentionDays.TWO_WEEKS,
                    removalPolicy: cdk.RemovalPolicy.DESTROY,
                }),
            }),
            environment: {
                STORAGE_BUCKET: props.storageBucket.bucketName,
                JOB_TABLE_NAME: "scverse-job-table",
                SNS_TOPIC_ARN: this.notificationTopic.topicArn,
            },
        });

        // --- Step Functions State Machine -----------------------------------
        const rawPipelineDefinition = fs.readFileSync(
            path.join(
                __dirname,
                "../../../../gateways/scverse/pipeline/pipeline_definition.json"
            ),
            "utf-8"
        );

        // Substitute placeholders with actual resource ARNs/values.
        // SubnetIds must be a JSON array of strings, not a bare string.
        const privateSubnets = props.vpc.selectSubnets({
            subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
        }).subnetIds;
        const subnetIdsJson = JSON.stringify(privateSubnets);

        const pipelineDefinition = rawPipelineDefinition
            .replace(/"\$\{SubnetIds\}"/g, subnetIdsJson)
            .replace(/\$\{SecurityGroupId\}/g, props.securityGroup.securityGroupId)
            .replace(/\$\{EcsClusterArn\}/g, this.ecsCluster.clusterArn)
            .replace(/\$\{EcsTaskDefinitionArn\}/g, this.fargateTaskDefinition.taskDefinitionArn)
            .replace(/\$\{ComputeLambdaArn\}/g, this.computeLambda.functionArn)
            .replace(/\$\{JobTableName\}/g, this.jobTable.tableName)
            .replace(/\$\{SnsTopicArn\}/g, this.notificationTopic.topicArn);

        this.pipelineStateMachine = new sfn.StateMachine(this, "PipelineStateMachine", {
            stateMachineName: "scverse-pipeline-orchestrator",
            definitionBody: sfn.DefinitionBody.fromString(pipelineDefinition),
            timeout: Duration.hours(4),
            tracingEnabled: true,
        });

        // --- VPC Gateway Endpoint for S3 ------------------------------------
        // Keeps S3 traffic on the AWS backbone; avoids NAT egress charges.
        props.vpc.addGatewayEndpoint("S3Endpoint", {
            service: ec2.GatewayVpcEndpointAwsService.S3,
            subnets: [{ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }],
        });

        // --- IAM Permissions -------------------------------------------------
        // Gateway Lambda → Compute Lambda
        this.computeLambda.grantInvoke(props.gatewayLambda);

        // Gateway Lambda + Fargate task role → DynamoDB
        this.jobTable.grantReadWriteData(props.gatewayLambda);
        this.jobTable.grantReadWriteData(this.fargateTaskDefinition.taskRole);

        // Gateway Lambda + Fargate task role → SNS publish
        this.notificationTopic.grantPublish(props.gatewayLambda);
        this.notificationTopic.grantPublish(this.fargateTaskDefinition.taskRole);

        // Compute Lambda → S3 job + dataset prefixes
        props.storageBucket.grantReadWrite(this.computeLambda, "scverse-jobs/*");
        props.storageBucket.grantReadWrite(this.computeLambda, "datasets/*");

        // Fargate task role → S3 (read/write job outputs, read datasets)
        props.storageBucket.grantReadWrite(this.fargateTaskDefinition.taskRole, "scverse-jobs/*");
        props.storageBucket.grantRead(this.fargateTaskDefinition.taskRole, "datasets/*");

        // Gateway Lambda → ECS RunTask on the worker task definition
        props.gatewayLambda.addToRolePolicy(
            new iam.PolicyStatement({
                actions: ["ecs:RunTask"],
                resources: [this.fargateTaskDefinition.taskDefinitionArn],
            })
        );
        props.gatewayLambda.addToRolePolicy(
            new iam.PolicyStatement({
                actions: ["iam:PassRole"],
                resources: [
                    this.fargateTaskDefinition.taskRole.roleArn,
                    this.fargateTaskDefinition.executionRole!.roleArn,
                ],
            })
        );

        // Gateway Lambda → Step Functions start execution
        this.pipelineStateMachine.grantStartExecution(props.gatewayLambda);

        // Step Functions → Compute Lambda, ECS, DynamoDB, SNS
        this.computeLambda.grantInvoke(this.pipelineStateMachine);
        this.jobTable.grantReadWriteData(this.pipelineStateMachine);
        this.notificationTopic.grantPublish(this.pipelineStateMachine);
        // Scope ECS to only this workflow's task definition and its tasks,
        // rather than every task in the account.
        const arnScope = cdk.Stack.of(this);
        this.pipelineStateMachine.addToRolePolicy(
            new iam.PolicyStatement({
                actions: ["ecs:RunTask"],
                resources: [this.fargateTaskDefinition.taskDefinitionArn],
            })
        );
        this.pipelineStateMachine.addToRolePolicy(
            new iam.PolicyStatement({
                actions: ["ecs:StopTask", "ecs:DescribeTasks"],
                resources: [
                    `arn:aws:ecs:${arnScope.region}:${arnScope.account}:task/${this.ecsCluster.clusterName}/*`,
                ],
            })
        );
        this.pipelineStateMachine.addToRolePolicy(
            new iam.PolicyStatement({
                actions: ["iam:PassRole"],
                resources: [
                    this.fargateTaskDefinition.taskRole.roleArn,
                    this.fargateTaskDefinition.executionRole!.roleArn,
                ],
            })
        );
        // Step Functions manages a single EventBridge rule for ECS task-state
        // change events when running tasks in .sync (RUN_JOB) mode. Scope to
        // exactly that managed rule instead of every rule in the account.
        this.pipelineStateMachine.addToRolePolicy(
            new iam.PolicyStatement({
                actions: ["events:PutTargets", "events:PutRule", "events:DescribeRule"],
                resources: [
                    `arn:aws:events:${arnScope.region}:${arnScope.account}:rule/StepFunctionsGetEventsForECSTaskRule`,
                ],
            })
        );

        // Gateway Lambda → SNS subscribe (to manage per-researcher subscriptions)
        props.gatewayLambda.addToRolePolicy(
            new iam.PolicyStatement({
                actions: ["sns:Subscribe", "sns:ListSubscriptionsByTopic"],
                resources: [this.notificationTopic.topicArn],
            })
        );

        // --- Gateway Lambda environment variables ---------------------------
        props.gatewayLambda.addEnvironment("JOB_TABLE_NAME", this.jobTable.tableName);
        props.gatewayLambda.addEnvironment("SNS_TOPIC_ARN", this.notificationTopic.topicArn);
        props.gatewayLambda.addEnvironment("COMPUTE_LAMBDA_ARN", this.computeLambda.functionArn);
        props.gatewayLambda.addEnvironment("ECS_CLUSTER_ARN", this.ecsCluster.clusterArn);
        props.gatewayLambda.addEnvironment(
            "ECS_TASK_DEFINITION_ARN",
            this.fargateTaskDefinition.taskDefinitionArn
        );
        props.gatewayLambda.addEnvironment(
            "STATE_MACHINE_ARN",
            this.pipelineStateMachine.stateMachineArn
        );
        props.gatewayLambda.addEnvironment(
            "PRIVATE_SUBNET_IDS",
            JSON.stringify(props.vpc.privateSubnets.map((s) => s.subnetId))
        );
        props.gatewayLambda.addEnvironment("SECURITY_GROUP_ID", props.securityGroup.securityGroupId);
        props.gatewayLambda.addEnvironment("DATASET_PREFIX", "datasets/");

        // --- S3 permissions for Gateway Lambda — datasets/* prefix ---------
        props.gatewayLambda.addToRolePolicy(
            new iam.PolicyStatement({
                actions: ["s3:ListBucket"],
                resources: [props.storageBucket.bucketArn],
                conditions: { StringLike: { "s3:prefix": ["datasets/*"] } },
            })
        );
        props.gatewayLambda.addToRolePolicy(
            new iam.PolicyStatement({
                actions: ["s3:HeadObject", "s3:GetObject"],
                resources: [props.storageBucket.arnForObjects("datasets/*")],
            })
        );

        // --- cdk-nag suppressions -------------------------------------------
        // Compute Lambda: managed policies for VPC execution; wildcard S3 on
        // scverse-jobs/* and datasets/* is intentional.
        NagSuppressions.addResourceSuppressions(
            this.computeLambda,
            [
                { id: "AwsSolutions-IAM4", reason: "Container Lambda uses AWSLambdaVPCAccessExecutionRole for ENI lifecycle management" },
                { id: "AwsSolutions-IAM5", reason: "S3 access scoped to scverse-jobs/* and datasets/* prefixes; DynamoDB scoped to the scverse-job-table" },
                { id: "AwsSolutions-L1", reason: "Runtime is pinned via the container image (public.ecr.aws/lambda/python:3.11)" },
            ],
            true
        );

        // Gateway Lambda role picks up wildcard grants from this construct
        // (S3 prefix grants, DynamoDB, SNS, Step Functions, ECS RunTask).
        NagSuppressions.addResourceSuppressions(
            props.gatewayLambda,
            [
                { id: "AwsSolutions-IAM4", reason: "Lambda uses managed VPC execution role" },
                { id: "AwsSolutions-IAM5", reason: "Wildcards scoped to scverse-jobs/*, datasets/*, the scverse-job-table, the pipeline state machine, the Fargate task definition, and the SNS topic" },
            ],
            true
        );

        // Fargate task role: same prefix-scoped wildcards apply.
        NagSuppressions.addResourceSuppressions(
            this.fargateTaskDefinition,
            [
                { id: "AwsSolutions-IAM4", reason: "Default ECS task execution role uses AmazonECSTaskExecutionRolePolicy" },
                { id: "AwsSolutions-IAM5", reason: "S3 wildcards scoped to scverse-jobs/* (rw) and datasets/* (read); DynamoDB scoped to the scverse-job-table" },
                { id: "AwsSolutions-ECS2", reason: "Task environment variables carry non-secret config (bucket, table name, SNS ARN); no credentials in env" },
            ],
            true
        );

        // SNS: the topic is encrypted with a customer-managed KMS key (see
        // JobNotificationKey above), so server-side encryption is enforced.
        // HTTPS-only enforcement isn't applicable to the email protocol.
        NagSuppressions.addResourceSuppressions(
            this.notificationTopic,
            [
                { id: "AwsSolutions-SNS3", reason: "Email protocol subscribers cannot use HTTPS-only enforcement; topic policy restricts publishers to this stack" },
            ],
            true
        );

        // Step Functions: logging is not enabled by default in this construct
        // to keep costs low; tracing is enabled for execution visibility.
        NagSuppressions.addResourceSuppressions(
            this.pipelineStateMachine,
            [
                { id: "AwsSolutions-SF1", reason: "X-Ray tracing is enabled; full ALL-level logging adds cost and duplicates the job timeline already captured in DynamoDB" },
                { id: "AwsSolutions-SF2", reason: "Tracing is enabled via tracingEnabled=true" },
            ],
            true
        );
    }
}

