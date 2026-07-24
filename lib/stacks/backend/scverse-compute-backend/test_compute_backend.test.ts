/**
 * CDK assertion tests for the ScverseComputeBackend construct.
 *
 * These tests synthesize the construct against a minimal harness stack
 * and verify the resulting CloudFormation template matches the design
 * contract: DynamoDB Job Table, Compute Lambda sizing, ECS Fargate task
 * definition, VPC Gateway Endpoint for S3, Gateway Lambda environment
 * wiring, and the IAM grants the Gateway Lambda needs to drive the
 * whole compute stack.
 *
 * Docker image assets are stubbed — we care about the CFN shape, not
 * about running `docker build` during unit tests.
 *
 * Validates: Requirements 12.1, 12.3, 12.4, 12.5, 12.6.
 */

import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as kms from "aws-cdk-lib/aws-kms";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as s3 from "aws-cdk-lib/aws-s3";
import { ScverseComputeBackend } from "./index";

/**
 * Stub Docker asset bundling so synth stays in-process.
 *
 * `lambda.DockerImageCode.fromImageAsset` and `ecs.ContainerImage.fromAsset`
 * both trigger `docker build` during synth. For CDK assertion tests we
 * replace them with references to a stub ECR repo / registry image —
 * synth produces a valid template, and our assertions don't depend on
 * the actual image contents.
 */
function stubDockerAssets(stack: cdk.Stack): void {
    const stubRepo = ecr.Repository.fromRepositoryName(stack, "StubRepo", "stub-repo");

    jest.spyOn(lambda.DockerImageCode, "fromImageAsset").mockImplementation(() =>
        lambda.DockerImageCode.fromEcr(stubRepo, { tagOrDigest: "stub" })
    );
    jest.spyOn(ecs.ContainerImage, "fromAsset").mockImplementation(
        () => ecs.ContainerImage.fromEcrRepository(stubRepo, "stub") as any
    );
}

function makeTestTemplate(): Template {
    const app = new cdk.App();
    const stack = new cdk.Stack(app, "TestStack", {
        env: { account: "123456789012", region: "us-west-2" },
    });

    stubDockerAssets(stack);

    const vpc = new ec2.Vpc(stack, "Vpc", { maxAzs: 2, natGateways: 1 });
    const securityGroup = new ec2.SecurityGroup(stack, "Sg", { vpc });
    const storageBucket = new s3.Bucket(stack, "Bucket");

    // Minimal Gateway Lambda stand-in — the construct wires env vars and
    // IAM onto whatever Function we pass in.
    const gatewayLambda = new lambda.Function(stack, "GatewayLambda", {
        runtime: lambda.Runtime.PYTHON_3_11,
        handler: "index.handler",
        code: lambda.Code.fromInline("def handler(event, context):\n    return {}\n"),
    });

    const logsKey = new kms.Key(stack, "LogsKey");

    new ScverseComputeBackend(stack, "ComputeBackend", {
        vpc,
        securityGroup,
        storageBucket,
        gatewayLambda,
        logsKey,
    });

    return Template.fromStack(stack);
}

describe("ScverseComputeBackend", () => {
    let template: Template;

    beforeAll(() => {
        template = makeTestTemplate();
    });

    afterAll(() => {
        jest.restoreAllMocks();
    });

    // ---- Req 12.1 — DynamoDB Job Table -----------------------------------
    test("DynamoDB Job Table uses PAY_PER_REQUEST and enables TTL on 'ttl'", () => {
        template.hasResourceProperties("AWS::DynamoDB::Table", {
            BillingMode: "PAY_PER_REQUEST",
            TimeToLiveSpecification: {
                AttributeName: "ttl",
                Enabled: true,
            },
        });
    });

    // ---- Req 12.3 — Compute Lambda sizing --------------------------------
    test("Compute Lambda reserves 10 concurrent executions, 10 GB memory, 900s timeout", () => {
        template.hasResourceProperties("AWS::Lambda::Function", {
            ReservedConcurrentExecutions: 10,
            MemorySize: 10240,
            Timeout: 900,
            PackageType: "Image",
        });
    });

    // ---- Req 12.3 — Fargate Task Definition ------------------------------
    test("Fargate task definition requests 4 vCPU / 16 GB", () => {
        // CloudFormation stores Cpu/Memory on TaskDefinition as strings.
        template.hasResourceProperties("AWS::ECS::TaskDefinition", {
            Cpu: "4096",
            Memory: "16384",
            RequiresCompatibilities: Match.arrayWith(["FARGATE"]),
        });
    });

    // ---- Req 12.4 — VPC Gateway Endpoint for S3 --------------------------
    test("VPC has a Gateway endpoint for S3", () => {
        // Multiple VPC endpoints may exist across resources; assert that
        // at least one Gateway endpoint targets the S3 service.
        template.hasResourceProperties("AWS::EC2::VPCEndpoint", {
            VpcEndpointType: "Gateway",
            ServiceName: {
                "Fn::Join": [
                    "",
                    Match.arrayWith([Match.stringLikeRegexp(".*\\.s3$")]),
                ],
            },
        });
    });

    // ---- Req 12.5 — Gateway Lambda environment wiring --------------------
    test("Gateway Lambda receives required compute-backend environment variables", () => {
        // The construct adds: JOB_TABLE_NAME, SNS_TOPIC_ARN, COMPUTE_LAMBDA_ARN,
        // ECS_CLUSTER_ARN, STATE_MACHINE_ARN (plus a few extras not asserted here).
        const requiredVars = [
            "JOB_TABLE_NAME",
            "SNS_TOPIC_ARN",
            "COMPUTE_LAMBDA_ARN",
            "ECS_CLUSTER_ARN",
            "STATE_MACHINE_ARN",
        ];

        const matcher: Record<string, unknown> = {};
        for (const name of requiredVars) {
            matcher[name] = Match.anyValue();
        }

        template.hasResourceProperties("AWS::Lambda::Function", {
            Handler: "index.handler",
            Environment: {
                Variables: Match.objectLike(matcher),
            },
        });
    });

    // ---- Req 12.6 — IAM policies on the Gateway Lambda role --------------
    //
    // CloudFormation renders `Action` as either a single string or an
    // array depending on how many actions a statement carries. Instead
    // of brittle shape matching, we dump the merged Statement arrays
    // from every IAM::Policy in the template and check the actions
    // appear at least once.

    const collectAllActions = (tmpl: Template): string[] => {
        const policies = tmpl.findResources("AWS::IAM::Policy");
        const actions: string[] = [];
        for (const policy of Object.values(policies)) {
            const statements = (policy as any).Properties?.PolicyDocument?.Statement ?? [];
            for (const stmt of statements) {
                if (stmt?.Effect !== "Allow") continue;
                const act = stmt.Action;
                if (Array.isArray(act)) actions.push(...act);
                else if (typeof act === "string") actions.push(act);
            }
        }
        return actions;
    };

    describe("Gateway Lambda IAM policies", () => {
        let allowedActions: string[];

        beforeAll(() => {
            allowedActions = collectAllActions(template);
        });

        test("can invoke the Compute Lambda", () => {
            expect(allowedActions).toContain("lambda:InvokeFunction");
        });

        test("can read and write the Job Table", () => {
            // grantReadWriteData expands to these action names.
            const requiredDdbActions = [
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
                "dynamodb:GetItem",
                "dynamodb:Query",
                "dynamodb:Scan",
            ];
            for (const action of requiredDdbActions) {
                expect(allowedActions).toContain(action);
            }
        });

        test("can publish to the notification SNS topic", () => {
            expect(allowedActions).toContain("sns:Publish");
        });

        test("can run ECS tasks on the worker task definition", () => {
            expect(allowedActions).toContain("ecs:RunTask");
        });

        test("can start executions on the pipeline state machine", () => {
            expect(allowedActions).toContain("states:StartExecution");
        });
    });
});
