# scverse Research Agent on Amazon Bedrock AgentCore

[![License: MIT-0](https://img.shields.io/badge/License-MIT--0-blue.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)
![Built on Amazon Bedrock AgentCore](https://img.shields.io/badge/Built%20on-Amazon%20Bedrock%20AgentCore-FF9900.svg)

> **Disclaimer:** This is a personal proof-of-concept, not an official AWS
> product, and it is not production-ready. See [DISCLAIMER.md](DISCLAIMER.md).

Talk to a single-cell genomics agent in plain English. Ask it to run quality
control on an `.h5ad` dataset and it executes real [scanpy](https://scanpy.readthedocs.io/)
compute on AWS, then streams the plots back into your chat inline. No scanpy or
boilerplate to write, and no AWS console to click through.

It is built on [Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/)
(Runtime + Gateway) and the [scverse](https://scverse.org/) ecosystem, and it works
with two frontends out of the box: a hosted chat UI (Open WebUI) and a Jupyter
notebook.

A researcher types:

> "Run QC on the PBMC dataset with a 5% mitochondrial threshold and show me the plots"

and gets three QC plots plus a filtered dataset back, produced by live compute on
their own data.

---

## See it in action

The same request in plain English, run live on the researcher's own PBMC3k
dataset across three frontends. Each streams the tool calls as the agent works
and renders the scanpy QC plots inline.

**Open WebUI**

> "Run QC on the PBMC dataset with a 5% mitochondrial threshold and show me the plots"

![scverse QC in Open WebUI](docs/images/openwebui-qc-section2.png)

**Jupyter notebook.** The same conversation from a notebook, with tool calls
printed live and plots as cell output.

![scverse QC in a Jupyter notebook](docs/images/jupyter-qc-section2.png)

**In your IDE.** Open the notebook in any editor with notebook support and run
it against your deployed agent.

![scverse QC in an IDE notebook](docs/images/ide-qc-section2.png)

---

## Who this is for

Researchers and teams who would rather drive real analysis from a chat interface
than write boilerplate, and builders who want a concrete, working example of the
Amazon Bedrock AgentCore pattern applied to a real scientific domain.

Single-cell QC is the example, but the pattern is the point. The same Runtime +
Gateway + MCP structure wraps anything you can put behind a Lambda or an MCP
server: a PubMed or literature-search agent, long-running analysis jobs, a
Redshift query agent, and more. Start from this one, swap in your own tools, and
the surrounding scaffolding carries over.

---

## Architecture

```mermaid
flowchart TB
    subgraph FE["Frontends (natural language)"]
        UI["Open WebUI"]
        NB["Jupyter notebook"]
    end

    UI --> PX["OpenAI-compatible proxy"]
    PX --> RT
    NB --> RT

    RT["AgentCore Runtime<br/>Strands agent + Claude"]
    RT -->|"MCP over HTTPS<br/>Cognito M2M auth"| GW["AgentCore Gateway"]
    GW --> LF["Gateway Lambda<br/>(tool router)"]

    LF -->|"run_scrna_qc, list_datasets"| CL["Container Lambda<br/>scanpy QC"]
    CL --> S3[("S3<br/>datasets + outputs")]
    S3 -.->|"presigned image URLs<br/>render inline"| RT

    subgraph ASYNC["Asynchronous compute (provisioned by the CDK, extend it)"]
        SFN["Step Functions"]
        FG["ECS Fargate<br/>full scverse stack"]
        DDB[("DynamoDB<br/>job tracking")]
        SNS["SNS<br/>notifications"]
    end
    LF -.->|"submit_scverse_job"| SFN
    SFN -.-> FG
    FG -.-> S3

    classDef verified fill:#e6f4ea,stroke:#137333,color:#0d652d;
    classDef provisioned fill:#f1f3f4,stroke:#9aa0a6,color:#5f6368,stroke-dasharray:4 3;
    class UI,NB,PX,RT,GW,LF,CL,S3 verified;
    class SFN,FG,DDB,SNS provisioned;
```

Three layers, each with one job:

- **Runtime** hosts the agent loop (Strands + Claude), the system prompt, and
  response streaming.
- **Gateway** exposes the scverse tools over MCP and routes each tool call to a
  Lambda. It authenticates the agent with Cognito M2M (client-credentials) by
  default; swap in any OIDC provider that issues standard JWTs.
- **Compute** runs the actual science. Synchronous QC runs on a container Lambda
  (green path above). The CDK also provisions an asynchronous path (Step
  Functions, ECS Fargate, DynamoDB, SNS) for large datasets and multi-step
  pipelines, ready for you to build on.

Plots never travel through the model. The compute layer writes them to S3 and
returns presigned URLs, which the frontends render inline.

---

## Prerequisites

- An AWS account with Amazon Bedrock enabled in `us-west-2`
- AWS CLI v2, configured so `aws sts get-caller-identity` works
- Node.js 22+ and npm
- Python 3.10+
- Docker (for building the agent and Lambda/Fargate container images)
  - On Apple Silicon, Docker Desktop with `buildx` is required for the
    `linux/amd64` image builds. Verify with `docker buildx ls`.

---

## Deploy

### 1. Clone and run the tests (no AWS needed)

```bash
git clone https://github.com/aws-samples/sample-agentcore-scverse.git
cd sample-agentcore-scverse
python3 -m pip install pytest==9.1.1 hypothesis==6.161.0 boto3==1.43.55
STORAGE_BUCKET=test-bucket pytest gateways -q
```

The unit and property-based tests run locally against mocked AWS clients.

### 2. Deploy the infrastructure

Edit `cdk.json` and replace the placeholder account with your 12-digit AWS
account ID:

```json
"accounts": {
  "dev": { "number": "123456789012", "region": "us-west-2" }
}
```

Then bootstrap and deploy:

```bash
npm install
npx cdk bootstrap aws://YOUR_ACCOUNT_ID/us-west-2
npx cdk deploy "dev/backend" --context stage=dev --require-approval never
```

This provisions a VPC (3 AZs, private subnets, NAT, flow logs), an encrypted S3
bucket, Cognito M2M auth, the AgentCore Gateway with its six tool schemas, the
container Lambda for synchronous QC, and the asynchronous compute stack (Step
Functions, ECS Fargate, DynamoDB, SNS). Note the CDK outputs; you will need
`ScverseGatewayUrl`, `ScverseGatewayId`, `StorageBucketName`, `UserPoolId`,
`UserPoolClientId`, `OAuthDomain`, and `ResourceServerId`.

### 3. Upload a sample dataset

This sample does not bundle a dataset. Download the public PBMC3k dataset
(2,700 peripheral blood mononuclear cells from 10x Genomics) with scanpy, then
upload it to the `datasets/` prefix the Gateway reads from:

```bash
python3 -m pip install scanpy
python3 -c "import scanpy as sc; sc.datasets.pbmc3k().write('pbmc3k_raw.h5ad')"

BUCKET=$(jq -r '.["dev-agentcore-scverse-backend"].StorageBucketName' cdk-outputs.json)
aws s3 cp pbmc3k_raw.h5ad s3://$BUCKET/datasets/pbmc3k_raw.h5ad
```

Drop your own `.h5ad` files into the same prefix and the agent discovers them
through the `list_datasets` tool.

### 4. Deploy the agent to AgentCore Runtime

```bash
cd agents/scverse-research-agent
pip install -r requirements.txt

export AGENTCORE_SCVERSE_GATEWAY_URL=<ScverseGatewayUrl>
export AGENTCORE_SCVERSE_GATEWAY_USER_POOL_ID=<UserPoolId>
export AGENTCORE_SCVERSE_GATEWAY_CLIENT_ID=<UserPoolClientId>
export AGENTCORE_SCVERSE_GATEWAY_OAUTH_DOMAIN=<OAuthDomain>
export AGENTCORE_SCVERSE_GATEWAY_RESOURCE_SERVER_ID=<ResourceServerId>
export AGENTCORE_SCVERSE_GATEWAY_ID=<ScverseGatewayId>

python deploy.py
```

This builds the agent image, pushes it to ECR, and registers the agent with
AgentCore Runtime wired to the Gateway. The agent discovers all six tools
automatically. The deploy writes the agent ARN to SSM at
`/agents/scverse_research_agent_arn`.

Smoke-test it from the CLI:

```bash
agentcore invoke '{"prompt": "What datasets are available?"}'
```

### 5. Open a frontend

Both frontends use your local AWS credential chain (via a read-only `~/.aws`
mount or your active profile). No credentials are typed into the app or passed
as environment variables. See [`frontend/README.md`](frontend/README.md) for the
full walkthrough of both options, including the Jupyter notebook setup.

**Open WebUI:**

```bash
cd frontend
export AGENTCORE_RUNTIME_ARN="<agent ARN from step 4>"
docker compose up --build -d
```

Open <http://localhost:3000>, pick **scverse-research-agent** as the model, and
chat. Tear down with `docker compose down`.

---

## Try it

Once a frontend is connected, these prompts exercise the verified path:

- **"What datasets are available?"**: lists the `.h5ad` files in your bucket.
- **"Run QC on the PBMC dataset with a 5% mitochondrial threshold and show me the plots"**:
  runs live scanpy QC and returns three plots plus a filtered dataset inline.
- **"Why were those cells filtered out? Was 5% too strict?"**: a conversational
  follow-up (the notebook threads context across turns).
- **"Which scvi-tools model should I use for CITE-seq data, and why?"**: the
  agent reasons through the model-selection decision tree.

As the agent works, the tools it calls (`list_datasets`, `run_scrna_qc`) surface
in the response so you can follow along while compute runs.

---

## Current status

This is an actively evolving proof-of-concept. The table reflects what has been
run end to end versus what the CDK provisions for you to build on.

| Capability | Status |
|---|---|
| Natural-language synchronous QC (scanpy) with inline plots | Verified |
| Dataset discovery (`list_datasets`) | Verified |
| Two frontends: Open WebUI and Jupyter notebook | Verified |
| AgentCore Runtime + Gateway (MCP tools, Cognito M2M auth) | Verified |
| scvi-tools model selection (reasoning, no compute) | Verified |
| Asynchronous jobs, ECS Fargate compute, Step Functions pipeline, DynamoDB tracking, SNS notifications | Provisioned by the CDK; a starting point for extension |

Conversational memory is threaded client-side in the notebook. For server-side
memory across sessions, enable AgentCore Memory on the Runtime (the agent code
already supports it).

---

## scverse tools

The MCP tool schemas registered with the Gateway live in
[`gateways/scverse/tool_description/tool-schema.json`](gateways/scverse/tool_description/tool-schema.json).

| Tool | Description | Status |
|---|---|---|
| `run_scrna_qc` | MAD-based QC on an `.h5ad` dataset (scanpy) | Verified (live compute) |
| `list_datasets` | List available `.h5ad` files in S3 | Verified |
| `run_scvi_analysis` | scvi-tools model selection across 7 models | Verified (model selection) |
| `submit_scverse_job` | Submit a long-running analysis job | Provisioned (async path) |
| `check_job_status` | Query a job's status by ID | Provisioned (async path) |
| `get_job_results` | Retrieve output presigned URLs for a job | Provisioned (async path) |

---

## Extending it

- **Add a tool.** Add a schema to `tool-schema.json`, implement the handler in
  the Gateway Lambda, and the agent picks it up on its next deploy.
- **Build out the async path.** The Step Functions state machine, Fargate task
  definition, DynamoDB table, and SNS topic are already deployed. Wire your
  heavier scverse workflows (integration, clustering, differential expression)
  into the compute container and route them through `submit_scverse_job`.
- **Swap the identity provider.** The Gateway accepts standard JWTs; point it at
  any OIDC provider instead of Cognito.

---

## Project structure

```
sample-agentcore-scverse/
├── agents/
│   ├── scverse-research-agent/   # Strands agent, system prompt, deploy.py
│   └── framework/                # Shared base agent + gateway utilities
├── gateways/scverse/
│   ├── function/                 # Gateway Lambda: tool router + tools + tests
│   ├── compute/                  # Container Lambda: scanpy QC/preprocessing
│   ├── fargate/                  # Fargate worker (async, full scverse stack)
│   ├── pipeline/                 # Step Functions definition + tests
│   └── tool_description/         # MCP tool schemas (6 tools)
├── frontend/
│   ├── proxy/                    # OpenAI-compatible AgentCore proxy
│   └── jupyter/                  # Conversational notebook
└── lib/stacks/backend/           # CDK: VPC, S3, Cognito, Gateway, compute
```

---

## Cost

- **NAT Gateway:** ~$35/month while the VPC is deployed (the main idle cost)
- **AgentCore Runtime, Lambda, Bedrock:** per-invocation and per-token
- **S3, DynamoDB, Cognito, SNS:** minimal at this scale

Tear everything down when you are done:

```bash
npx cdk destroy "dev/backend" --context stage=dev
```

---

## Security

This sample uses Cognito M2M auth on the Gateway, scoped IAM roles, an encrypted
S3 bucket with public access blocked, and a VPC with private subnets. Review the
IAM policies and network configuration against your own requirements before
using any of it beyond experimentation.

See [docs/threat-model.md](docs/threat-model.md) for the threat model (trust
boundaries, STRIDE threats, and mitigations), and
[CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines.

## License

MIT-0. See [LICENSE](LICENSE).
