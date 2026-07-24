import * as dotenv from "dotenv";
import { App, Tags } from "aws-cdk-lib";
import { ApplicationStage } from "../lib/stage";

dotenv.config();

const app = new App({});

const projectId = app.node.tryGetContext("projectId");
if (projectId) Tags.of(app).add("projectId", projectId);

const stage = app.node.tryGetContext("stage") || "dev";
const account = app.node.tryGetContext("accounts")?.[stage];

new ApplicationStage(app, stage, {
    env: {
        account: account?.number,
        region: account?.region,
    },
});

app.synth();
