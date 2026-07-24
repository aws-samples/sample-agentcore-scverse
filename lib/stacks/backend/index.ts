import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";
import { AgentcoreScverseGateway } from "./agentcore-scverse-gateway";
import { ScverseComputeBackend } from "./scverse-compute-backend";

export class Backend extends cdk.Stack {
    constructor(scope: Construct, id: string, props?: cdk.StackProps) {
        super(scope, id, {
            ...props,
            stackName: `${scope.node.id}-agentcore-scverse-backend`,
        });

        // VPC with private subnets for Lambda
        const vpc = new ec2.Vpc(this, "Vpc", {
            maxAzs: 3,
            natGateways: 1,
            subnetConfiguration: [
                { name: "public", subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
                { name: "private", subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: 24 },
            ],
        });

        // Customer-managed KMS key for CloudWatch Logs (VPC flow logs, Lambda,
        // and ECS task logs). CloudWatch Logs requires a key policy granting the
        // logs service principal use of the key, scoped by encryption context to
        // this account's log groups.
        const logsKey = new kms.Key(this, "LogsKey", {
            description: "Encrypts scverse CloudWatch log groups",
            enableKeyRotation: true,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
        });
        logsKey.addToResourcePolicy(
            new iam.PolicyStatement({
                principals: [new iam.ServicePrincipal(`logs.${this.region}.amazonaws.com`)],
                actions: [
                    "kms:Encrypt",
                    "kms:Decrypt",
                    "kms:ReEncrypt*",
                    "kms:GenerateDataKey*",
                    "kms:DescribeKey",
                ],
                resources: ["*"],
                conditions: {
                    ArnLike: {
                        "kms:EncryptionContext:aws:logs:arn": `arn:aws:logs:${this.region}:${this.account}:log-group:*`,
                    },
                },
            })
        );

        // VPC Flow Logs (required by AwsSolutions-VPC7), encrypted with the CMK.
        const flowLogGroup = new logs.LogGroup(this, "VpcFlowLogGroup", {
            encryptionKey: logsKey,
            retention: logs.RetentionDays.TWO_WEEKS,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
        });
        vpc.addFlowLog("FlowLog", {
            destination: ec2.FlowLogDestination.toCloudWatchLogs(flowLogGroup),
        });

        const securityGroup = new ec2.SecurityGroup(this, "LambdaSg", {
            vpc,
            description: "Security group for scverse gateway Lambda",
            allowAllOutbound: true,
        });

        // S3 access logs bucket
        const accessLogsBucket = new s3.Bucket(this, "AccessLogsBucket", {
            removalPolicy: cdk.RemovalPolicy.DESTROY,
            autoDeleteObjects: true,
            encryption: s3.BucketEncryption.S3_MANAGED,
            blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
            enforceSSL: true,
        });

        // Customer-managed KMS key for the storage bucket, so dataset and output
        // objects are encrypted with a key the customer controls, rotates, and
        // audits. bucketKeyEnabled reduces KMS request costs via an S3 Bucket Key.
        const storageKey = new kms.Key(this, "StorageBucketKey", {
            description: "Encrypts scverse dataset and output objects",
            enableKeyRotation: true,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
        });

        // S3 bucket for input data and output results (SSE-KMS with the CMK above).
        const storageBucket = new s3.Bucket(this, "StorageBucket", {
            bucketName: cdk.PhysicalName.GENERATE_IF_NEEDED,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
            autoDeleteObjects: true,
            encryption: s3.BucketEncryption.KMS,
            encryptionKey: storageKey,
            bucketKeyEnabled: true,
            blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
            enforceSSL: true,
            serverAccessLogsBucket: accessLogsBucket,
            serverAccessLogsPrefix: "storage-bucket-logs/",
        });

        // scverse Gateway
        const scverseGateway = new AgentcoreScverseGateway(this, "ScverseGateway", {
            vpc,
            securityGroup,
            storageBucket,
            logsKey,
        });

        // Compute backend: DynamoDB, SNS, Compute Lambda, ECS Fargate,
        // Step Functions pipeline, and all IAM wiring the Gateway needs.
        const computeBackend = new ScverseComputeBackend(this, "ComputeBackend", {
            vpc,
            securityGroup,
            storageBucket,
            gatewayLambda: scverseGateway.lambdaFunction,
            logsKey,
        });

        // Outputs for agent configuration
        new cdk.CfnOutput(this, "ScverseGatewayUrl", {
            value: scverseGateway.gatewayUrl,
            description: "scverse Gateway URL for agent configuration",
        });
        new cdk.CfnOutput(this, "ScverseGatewayId", {
            value: scverseGateway.gatewayId,
        });
        new cdk.CfnOutput(this, "StorageBucketName", {
            value: storageBucket.bucketName,
        });
        new cdk.CfnOutput(this, "UserPoolId", {
            value: scverseGateway.userPool.userPoolId,
        });
        new cdk.CfnOutput(this, "UserPoolClientId", {
            value: scverseGateway.userPoolClient.userPoolClientId,
        });
        new cdk.CfnOutput(this, "OAuthDomain", {
            value: scverseGateway.oauthDomain,
            description: "Cognito OAuth domain prefix for token endpoint",
        });
        new cdk.CfnOutput(this, "ResourceServerId", {
            value: scverseGateway.resourceServerId,
            description: "Resource server ID for OAuth scopes",
        });

        // Compute backend outputs
        new cdk.CfnOutput(this, "JobTableName", {
            value: computeBackend.jobTable.tableName,
        });
        new cdk.CfnOutput(this, "ComputeLambdaArn", {
            value: computeBackend.computeLambda.functionArn,
        });
        new cdk.CfnOutput(this, "EcsClusterArn", {
            value: computeBackend.ecsCluster.clusterArn,
        });
        new cdk.CfnOutput(this, "StateMachineArn", {
            value: computeBackend.pipelineStateMachine.stateMachineArn,
        });
        new cdk.CfnOutput(this, "SnsTopicArn", {
            value: computeBackend.notificationTopic.topicArn,
        });
    }
}
