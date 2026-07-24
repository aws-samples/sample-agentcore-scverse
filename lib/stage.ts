import { Aspects, Stage, StageProps } from "aws-cdk-lib";
import { AwsSolutionsChecks, NagSuppressions } from "cdk-nag";
import { Construct } from "constructs";
import { Backend } from "./stacks/backend";

export class ApplicationStage extends Stage {
    constructor(scope: Construct, id: string, props?: StageProps) {
        super(scope, id, props);

        new Backend(this, "backend");

        NagSuppressions.addResourceSuppressions(
            this,
            [
                {
                    id: "AwsSolutions-IAM4",
                    reason: "Lambda functions require AWSLambdaBasicExecutionRole for CloudWatch logs.",
                    appliesTo: [
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                    ],
                },
                {
                    id: "AwsSolutions-IAM5",
                    reason: "High-level constructs require wildcards for dynamic resource management.",
                },
                {
                    id: "AwsSolutions-L1",
                    reason: "High-level constructs set their own runtimes.",
                },
                {
                    id: "AwsSolutions-S1",
                    reason: "Access logs bucket does not need its own access logs (circular).",
                    appliesTo: ["Resource::*AccessLogsBucket*"],
                },
            ],
            true
        );
        Aspects.of(this).add(new AwsSolutionsChecks());
    }
}
