/**
 * AgentCore scverse Gateway Construct
 *
 * Lambda-backed gateway exposing scverse tools:
 *   - run_scrna_qc          (scanpy QC on .h5ad)
 *   - run_scvi_analysis     (scvi-tools model selection + analysis)
 *   - submit_scverse_job    (async job submission)
 *   - check_job_status      (job status query)
 *   - get_job_results       (presigned URL retrieval)
 *
 * Uses Cognito M2M auth + AgentCore Gateway pattern.
 */

import * as path from "path";
import * as cdk from "aws-cdk-lib";
import { Duration, Fn, Stack } from "aws-cdk-lib";
import * as agentcore from "@aws-cdk/aws-bedrock-agentcore-alpha";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { NagSuppressions } from "cdk-nag";
import { Construct } from "constructs";

export interface AgentcoreScverseGatewayProps {
    vpc: ec2.IVpc;
    securityGroup: ec2.ISecurityGroup;
    storageBucket: s3.IBucket;
    logsKey: kms.IKey;
}

export class AgentcoreScverseGateway extends Construct {
    public readonly gateway: agentcore.Gateway;
    public readonly gatewayId: string;
    public readonly gatewayUrl: string;
    public readonly lambdaFunction: lambda.Function;
    public readonly userPool: cognito.UserPool;
    public readonly userPoolClient: cognito.UserPoolClient;
    public readonly oauthDomain: string;
    public readonly resourceServerId: string;

    constructor(scope: Construct, id: string, props: AgentcoreScverseGatewayProps) {
        super(scope, id);

        const { vpc, securityGroup, storageBucket, logsKey } = props;
        const region = Stack.of(this).region;
        const accountId = Stack.of(this).account;
        const gatewayName = "ScverseGateway";

        // Lambda function — uses inline code (no Docker bundling needed)
        // boto3 and requests are available in the Python 3.11 Lambda runtime
        this.lambdaFunction = new lambda.Function(this, "ScverseGatewayLambda", {
            functionName: "mcp-server-scverse-gateway",
            runtime: lambda.Runtime.PYTHON_3_11,
            handler: "lambda_function.lambda_handler",
            code: lambda.Code.fromAsset(
                path.join(__dirname, "../../../../gateways/scverse/function"),
                {
                    exclude: ["test_*", "__pycache__", ".pytest_cache", "*.pyc"],
                }
            ),
            memorySize: 512,
            timeout: Duration.seconds(300),
            description: "scverse Gateway with scanpy QC, scvi-tools, and job management tools",
            environment: {
                LOG_LEVEL: "INFO",
                STORAGE_BUCKET: storageBucket.bucketName,
            },
            logGroup: new logs.LogGroup(this, "ScverseGatewayLambdaLogs", {
                logGroupName: "/aws/lambda/mcp-server-scverse-gateway",
                encryptionKey: logsKey,
                retention: logs.RetentionDays.TWO_WEEKS,
                removalPolicy: cdk.RemovalPolicy.DESTROY,
            }),
            vpc,
            securityGroups: [securityGroup],
            vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        });

        // S3 permissions
        this.lambdaFunction.addToRolePolicy(
            new iam.PolicyStatement({
                effect: iam.Effect.ALLOW,
                actions: ["s3:GetObject", "s3:PutObject"],
                resources: [
                    storageBucket.arnForObjects("datasets/*"),
                    storageBucket.arnForObjects("scverse-outputs/*"),
                    storageBucket.arnForObjects("scverse-jobs/*"),
                ],
            })
        );
        this.lambdaFunction.addToRolePolicy(
            new iam.PolicyStatement({
                effect: iam.Effect.ALLOW,
                actions: ["s3:ListBucket"],
                resources: [storageBucket.bucketArn],
                conditions: {
                    StringLike: {
                        "s3:prefix": ["datasets/*", "scverse-outputs/*", "scverse-jobs/*"],
                    },
                },
            })
        );

        // The bucket is encrypted with a customer-managed KMS key, so the Lambda
        // needs KMS permissions to read and write objects.
        storageBucket.encryptionKey?.grantEncryptDecrypt(this.lambdaFunction);

        // Cognito M2M auth
        this.userPool = new cognito.UserPool(this, "GatewayUserPool", {
            userPoolName: "scverse-agentcore-gateway-pool",
            selfSignUpEnabled: false,
            signInAliases: { email: true },
            removalPolicy: cdk.RemovalPolicy.DESTROY,
        });

        this.oauthDomain = `scverse-gateway-${accountId}`.toLowerCase();
        new cognito.UserPoolDomain(this, "GatewayUserPoolDomain", {
            userPool: this.userPool,
            cognitoDomain: { domainPrefix: this.oauthDomain },
        });

        this.resourceServerId = "scverse-agentcore-gateway-id";
        const resourceServer = new cognito.UserPoolResourceServer(this, "GatewayResourceServer", {
            userPool: this.userPool,
            identifier: this.resourceServerId,
            userPoolResourceServerName: "scverse-agentcore-gateway-name",
            scopes: [
                new cognito.ResourceServerScope({ scopeName: "gateway:read", scopeDescription: "Read access" }),
                new cognito.ResourceServerScope({ scopeName: "gateway:write", scopeDescription: "Write access" }),
            ],
        });

        this.userPoolClient = new cognito.UserPoolClient(this, "GatewayM2MClient", {
            userPool: this.userPool,
            userPoolClientName: "scverse-agentcore-gateway-client",
            generateSecret: true,
            authFlows: { custom: true },
            oAuth: {
                flows: { clientCredentials: true },
                scopes: [
                    cognito.OAuthScope.resourceServer(resourceServer,
                        new cognito.ResourceServerScope({ scopeName: "gateway:read", scopeDescription: "Read" })),
                    cognito.OAuthScope.resourceServer(resourceServer,
                        new cognito.ResourceServerScope({ scopeName: "gateway:write", scopeDescription: "Write" })),
                ],
            },
        });
        this.userPoolClient.node.addDependency(resourceServer);

        // AgentCore Gateway
        this.gateway = new agentcore.Gateway(this, "Gateway", {
            gatewayName,
            description: "AgentCore Gateway for scverse single-cell genomics tools",
            authorizerConfiguration: agentcore.GatewayAuthorizer.usingCognito({
                userPool: this.userPool,
                allowedClients: [this.userPoolClient],
            }),
        });

        this.gatewayId = this.gateway.gatewayId;
        this.gatewayUrl = this.gateway.gatewayUrl ?? "";

        agentcore.GatewayTarget.forLambda(this, "LambdaTarget", {
            gatewayTargetName: "ScverseLambdaTarget",
            description: "scverse Lambda Target with single-cell genomics tools",
            gateway: this.gateway,
            lambdaFunction: this.lambdaFunction,
            toolSchema: agentcore.ToolSchema.fromLocalAsset(
                path.join(__dirname, "../../../../gateways/scverse/tool_description/tool-schema.json")
            ),
        });

        // SSM for service discovery
        new ssm.StringParameter(this, "GatewayConfig", {
            parameterName: "/gateways/scverse-gateway",
            stringValue: Fn.sub(JSON.stringify({
                gateway_id: "${GatewayId}", gateway_url: "${GatewayUrl}",
                user_pool_id: "${UserPoolId}", client_id: "${ClientId}",
                oauth_domain: "${OAuthDomain}", resource_server_id: "${ResourceServerId}",
                tools_count: 5,
            }), {
                GatewayId: this.gatewayId, GatewayUrl: this.gatewayUrl,
                UserPoolId: this.userPool.userPoolId, ClientId: this.userPoolClient.userPoolClientId,
                OAuthDomain: this.oauthDomain, ResourceServerId: this.resourceServerId,
            }),
            description: "scverse Gateway configuration for service discovery",
        });

        // cdk-nag suppressions
        NagSuppressions.addResourceSuppressions(this.lambdaFunction, [
            { id: "AwsSolutions-IAM4", reason: "Lambda requires managed policies for VPC access" },
            { id: "AwsSolutions-IAM5", reason: "S3 access scoped to scverse-* prefixes" },
        ], true);
        NagSuppressions.addResourceSuppressions(this.userPool, [
            { id: "AwsSolutions-COG1", reason: "M2M auth — password policy not applicable" },
            { id: "AwsSolutions-COG2", reason: "M2M client credentials — MFA not applicable" },
            { id: "AwsSolutions-COG3", reason: "AdvancedSecurityMode not required for M2M" },
        ], true);
    }
}
