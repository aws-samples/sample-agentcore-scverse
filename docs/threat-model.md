# Threat Model — scverse Research Agent on Amazon Bedrock AgentCore

Prepared as part of the security review prior to open-source publication. It
follows a standard threat-modeling approach: describe the system, identify
trust boundaries and data flows, enumerate threats (STRIDE), and record
mitigations and residual risks.

> **This is sample code for non-production, educational use.** See
> [DISCLAIMER.md](../DISCLAIMER.md). You should work with your security and
> legal teams to meet your organizational security, regulatory, and compliance
> requirements before deploying it.

---

## 1. What we are building

A natural-language agent that runs single-cell genomics workflows
([scanpy](https://scanpy.readthedocs.io/) /
[scvi-tools](https://scvi-tools.org/)) on AWS via
[Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/). A
researcher asks, in plain English, to run quality control on an `.h5ad`
dataset; the agent executes real compute on AWS and streams the resulting plots
back into the chat. Two frontends ship with it: a hosted chat UI (Open WebUI)
and a Jupyter notebook.

Three layers (see the architecture diagram in [README.md](../README.md)):

- **Runtime** — AgentCore Runtime hosts the agent loop (Strands + Claude via
  Amazon Bedrock), the system prompt, and response streaming.
- **Gateway** — AgentCore Gateway exposes the scverse tools over MCP and routes
  each call to the Gateway Lambda. It authenticates the agent with Amazon
  Cognito machine-to-machine (client-credentials) by default; any OIDC provider
  issuing standard JWTs can be substituted.
- **Compute** — synchronous QC runs on a container Lambda; the CDK also
  provisions an asynchronous path (Step Functions, ECS Fargate, DynamoDB, SNS)
  for larger datasets and multi-step pipelines.

Plots never travel through the model: the compute layer writes them to S3 and
returns short-lived presigned URLs that the frontends render inline.

## 2. Assumptions

- A builder deploys this into **their own AWS account** for experimentation.
- Frontends run **locally** on the builder's machine (Open WebUI via Docker,
  or the Jupyter notebook) and use the builder's own AWS credential chain.
- Datasets are the builder's own and are uploaded to the builder's S3 bucket;
  they **may contain sensitive genomics data** (potentially PHI/PII depending
  on source and jurisdiction).
- The builder is responsible for production hardening — authentication, network
  exposure, and regulatory compliance — before any non-local use.

## 3. Data classification

| Data | Sensitivity | Where |
|---|---|---|
| `.h5ad` single-cell datasets | Potentially sensitive (PHI/PII depending on source) | S3 (`datasets/`), compute `/tmp`, Fargate |
| QC / analysis outputs (plots, filtered `.h5ad`) | Derived from above | S3 (`scverse-jobs/{job_id}/`), presigned URLs |
| Notification email (optional) | PII | Job request, DynamoDB job record |
| AWS account/resource identifiers | Low | Config, logs |
| Cognito M2M client credentials / JWTs | Secret | Gateway auth exchange |

## 4. Trust boundaries and data flow

| # | Boundary | Control |
|---|---|---|
| TB1 | External user ↔ Frontend (Open WebUI / Jupyter) | **Local-only by default; unauthenticated (`WEBUI_AUTH=false`).** Builder must add auth before any network exposure. |
| TB2 | Frontend / proxy ↔ AgentCore Runtime | AWS SigV4 using the builder's local credential chain (read-only `~/.aws` mount); no credentials in app config. |
| TB3 | AgentCore Runtime ↔ Gateway | Cognito M2M OAuth (client-credentials); Gateway validates the JWT. Pluggable OIDC. |
| TB4 | Gateway Lambda ↔ Compute (Lambda / ECS / Step Functions) | Scoped IAM roles; size-based routing; input validation before dispatch. |
| TB5 | Compute ↔ S3 / DynamoDB / SNS | Task/function roles scoped to the job prefix and named resources; encryption at rest. |
| TB6 | AgentCore Runtime ↔ Amazon Bedrock | First-party AWS service; IAM-authorized model inference. No third-party GenAI. |

Primary flow: user prompt → frontend → (proxy) → AgentCore Runtime → agent
reasoning (Bedrock) → tool call → Gateway (MCP, Cognito) → Gateway Lambda →
route by dataset size / workflow → Container Lambda (sync QC) or Fargate/Step
Functions (async) → read dataset from S3, compute, write outputs to a
job-namespaced S3 prefix, update DynamoDB, optionally notify via SNS →
presigned URLs returned → rendered inline in the frontend.

## 5. Threats and mitigations (STRIDE)

Severity reflects the risk **in the sample as published**; residual risk is what
the deploying builder owns.

### T1 — Unauthenticated access to the frontend (Spoofing / EoP)
**Risk:** The bundled Open WebUI runs with `WEBUI_AUTH=false`. If exposed beyond
localhost, anyone reaching it could invoke the agent, run compute against the
builder's data, and incur cost.
**Mitigation:** Documented as a local-demo default. `DISCLAIMER.md` and the
README give concrete auth patterns (Amazon Cognito, API Gateway authorizer,
ALB + OIDC) and instruct the builder to add authn/authz before any exposure.
The Gateway itself always requires Cognito M2M, so the tool layer is not open.
**Residual:** Builder is responsible for fronting the UI with auth before
network exposure. Accepted for a local sample.

### T2 — Cross-site scripting via rendered agent output (Tampering)
**Risk:** Agent responses are rendered as Markdown in the notebook; a malicious
or manipulated response containing raw HTML/JS could execute in the notebook.
**Mitigation:** A `_safe_md()` sanitizer strips real HTML tags and neutralizes
`javascript:` / `data:` / `vbscript:` link schemes before rendering; plot images
are extracted and rendered via `Image()` separately. Verified
against XSS payloads and legitimate genomics prose.
**Residual:** Low.

### T3 — Prompt injection into tool calls (Tampering / EoP)
**Risk:** User prompts or dataset-derived content could steer the agent toward
unintended actions.
**Mitigation:** The agent exposes only a small fixed allowlist of scverse tools
(QC, model selection, dataset listing, job management) — no arbitrary code
execution. The Gateway Lambda validates every request before dispatch:
`workflow_type` against an allowlist, `input_s3_key` existence (S3 HEAD),
`.h5ad` extension, and email format. Compute runs in sandboxed Lambda/Fargate
with scoped roles.
**Residual:** The model may still call a valid tool with unexpected arguments;
bounded by input validation and least-privilege compute. Accepted.

### T4 — Sensitive data in logs (Information Disclosure)
**Risk:** Handlers logging full request payloads or exceptions could write user
data (notification email, S3 keys, prompts, parameters) to CloudWatch.
**Mitigation:** Logging narrowed across the gateway, agent framework, and
Fargate worker to record only error type and safe metadata (tool name, request
id, job id) — never the raw event or full exception.
**Residual:** Low. A job's own error string is still stored on its DynamoDB
record and returned to the requesting researcher (their own data), which is
acceptable.

### T5 — Exposure of analysis outputs in S3 (Information Disclosure)
**Risk:** Presigned URLs to outputs could leak or be over-scoped.
**Mitigation:** Presigned URLs expire in 1 hour, are generated fresh per request
(never cached), and each job's outputs live under an isolated
`scverse-jobs/{job_id}/` prefix. The S3 bucket blocks public access and is
encrypted at rest with a customer-managed KMS key.
**Residual:** Low.

### T6 — Sensitive data at rest / regulatory exposure (Information Disclosure)
**Risk:** `.h5ad` datasets may contain PHI/PII; mishandling could breach HIPAA,
GDPR, or similar.
**Mitigation:** All data stores are encrypted at rest with dedicated
customer-managed KMS keys (rotation enabled): the S3 storage bucket (SSE-KMS
with an S3 Bucket Key), the DynamoDB job table, the SNS notification topic, and
the CloudWatch log groups (VPC flow logs, Lambda, and ECS task logs). The S3
bucket also blocks public access and enforces TLS. `DISCLAIMER.md` states the
builder is responsible for compliance controls before processing regulated data.
**Residual:** The builder owns HIPAA/GDPR controls (BAAs, access logging, audit
trails). Documented and accepted for a sample.

### T7 — Over-broad IAM permissions (Elevation of Privilege)
**Risk:** Wildcard IAM in copied constructs could grant account-wide access.
**Mitigation:** Step Functions IAM scoped to least privilege — `ecs:RunTask` on
the worker task definition, `ecs:StopTask`/`DescribeTasks` on tasks in the
cluster, and EventBridge actions on the Step Functions-managed rule only.
Compute roles are scoped to the job prefix and named
resources.
**Residual:** The agent runtime IAM template grants Cognito `InitiateAuth` on a
wildcard resource (a discovery action that is hard to scope without a
per-deployment user-pool ARN) — accepted MEDIUM; builders should scope it to
their pool ARN.

### T8 — Denial of service / cost abuse (DoS)
**Risk:** An unauthenticated frontend plus async jobs could be abused to run
expensive Fargate/Bedrock workloads.
**Mitigation:** Container Lambda reserved concurrency (10), Fargate max tasks
(50), scale-to-zero compute, a 300s gateway timeout budget with fallback to the
async path, and size-based routing that keeps small jobs on cheap sync compute.
**Residual:** Cost guardrails (budgets, alarms) and frontend auth are the
deploying builder's responsibility. Accepted for a sample.

### T9 — Vulnerable or drifting dependencies (Tampering / Supply Chain)
**Risk:** Vulnerable third-party packages or unpinned versions could introduce
known CVEs.
**Mitigation:** Runtime dependencies pinned; `jinja2` bumped to `3.1.6` to
resolve CVE-2024-56326 / CVE-2024-34064; container base images pulled from
Amazon ECR Public rather than external registries.
**Residual:** Some agent-framework dependencies use `>=` ranges and the CDK
`package.json` uses caret ranges with a committed `package-lock.json` — standard
sample convention, accepted MEDIUM.

### T10 — Job integrity / lost results (Tampering / repudiation)
**Risk:** A job could report complete while its outputs are missing, or fail
silently.
**Mitigation:** Output keys are written to DynamoDB **before** status flips to
`COMPLETED`; every async job gets a DynamoDB record at submission; Fargate
writes a heartbeat every 30s (a stale heartbeat while `RUNNING` is surfaced, not
hidden); jobs are isolated by job id in both S3 and DynamoDB.
**Residual:** Low.

## 6. Accepted residual risks (from the security scan)

All **HIGH** findings are remediated (HIGH = 0). The following MEDIUM/LOW
findings are consciously accepted for a non-production sample and are the
deploying builder's responsibility to harden:

- Dependency version ranges in the agent `requirements.txt` and `package.json`
  (mitigated by `package-lock.json`; no CVE-backed findings remain).
- Cognito `InitiateAuth` wildcard in the agent IAM template (T7).
- Direct Jinja2 use in the (deferred) Fargate HTML-report render.
- `WEBUI_AUTH=false` local-demo default (T1).
- A HIPAA/PHI compliance-note suggestion for the README (T6).

## 7. Security review summary

This sample was reviewed with an automated content-security scan and a manual
security review prior to publication:

- **Result:** all HIGH-severity findings remediated (HIGH = 0, reduced from an
  initial 6). The remaining MEDIUM/LOW findings are the consciously accepted,
  non-production residual risks documented in section 6.
- **Encryption hardening:** the S3 storage bucket, DynamoDB job table, SNS
  topic, and CloudWatch log groups (VPC flow logs, Lambda, ECS) use dedicated
  customer-managed KMS keys with rotation enabled; local frontend ports
  (Open WebUI, proxy) are bound to `127.0.0.1`.
