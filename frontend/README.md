# Frontend Options

Two natural-language surfaces for the scverse agent. Both talk to the deployed
**AgentCore Runtime** agent (which routes through the Gateway to Lambda/Fargate/
Step Functions), and both render QC plots inline. Pick based on your audience.

| Surface | Best for | Renders plots inline |
|---|---|---|
| **Open WebUI** | Demos, non-technical users, a hosted chat UI | Yes |
| **Jupyter notebook** | Data scientists who live in notebooks | Yes |

Both use your **local AWS credential chain** — no credentials are typed into the
app or passed as environment variables.

---

## Prerequisites (both surfaces)

1. The stack is deployed (`cdk deploy`) and the agent is deployed to AgentCore
   Runtime (`agents/scverse-research-agent/deploy.py`).
2. Local AWS credentials for that account. Verify:

   ```bash
   aws sts get-caller-identity   # must return the account where the stack lives
   ```

> **Credential gotcha (important):** do **not** `export AWS_ACCESS_KEY_ID=...`
> with placeholder or stale values in your shell. Empty or bogus AWS env vars
> override `~/.aws/credentials` and cause `IncompleteSignatureException:
> Invalid key=value pair ... in Authorization header`. If you see that error,
> run `unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN` and rely
> on your `~/.aws` profile (or SSO/ada session) instead.

---

## Option 1: Open WebUI

A two-container stack: Open WebUI (the chat UI) plus a thin proxy that translates
OpenAI chat-completions calls into AgentCore `invoke_agent_runtime` calls. The
proxy authenticates with the standard AWS credential chain via a read-only mount
of `~/.aws` — no tokens are passed as environment variables.

```bash
cd frontend
export AGENTCORE_RUNTIME_ARN="<your agent runtime ARN>"
docker compose up --build -d
```

Open <http://localhost:3000>, pick **scverse-research-agent** as the model, and chat:

- "What datasets are available?"
- "Run QC on the PBMC dataset with a 5% mitochondrial threshold and show me the plots"
- "Which scvi-tools model should I use for CITE-seq data?"

Tear down with `docker compose down`.

**How it works**

```
Open WebUI  →  proxy (OpenAI ↔ AgentCore)  →  AgentCore Runtime  →  Gateway (MCP)  →  compute  →  S3
```

The agent returns plots as markdown image references (`![](presigned_url)`), which
Open WebUI renders inline. Find the runtime ARN in your CDK/agent deploy output,
or read it from SSM: `aws ssm get-parameter --name /agents/scverse_research_agent_arn`.

---

## Option 2: Jupyter Notebook

A conversational notebook for data scientists. You type plain English; the agent
runs live compute, the tools it calls (`list_datasets`, `run_scrna_qc`, ...) print
live so you can follow progress, and plots render inline as cell output. No scanpy
code required.

### One-time setup (isolated environment)

Use a dedicated virtual environment so a stale system Jupyter can't interfere. A
system Jupyter tied to an old Python (e.g. 3.9) commonly fails with
`field() got an unexpected keyword argument 'alias'` or `jsonschema` /
`referencing` import errors. An isolated venv avoids that entirely.

```bash
cd frontend/jupyter
python3.11 -m venv .venv                       # any Python 3.10+ works
.venv/bin/python -m pip install --upgrade "pip==26.1.2"
.venv/bin/python -m pip install jupyterlab==4.6.2 ipykernel==7.3.0 boto3==1.43.55

# Register this environment as a Jupyter kernel (needed to run inside an IDE)
.venv/bin/python -m ipykernel install --user \
  --name scverse-agentcore --display-name "Python (scverse-agentcore)"
```

You can then run the notebook either in a browser or inside your IDE.

### Run it in a browser (Jupyter server)

```bash
.venv/bin/jupyter lab scverse_agent_notebook.ipynb
```

Jupyter opens in your browser. If prompted for a kernel, choose **Python
(scverse-agentcore)**, then run the cells top to bottom.

### Run it in an IDE

Open `frontend/jupyter/scverse_agent_notebook.ipynb` in any editor with notebook
support, then use the editor's kernel picker to select **Python
(scverse-agentcore)** (the kernel you registered above) and run the cells top to
bottom. The notebook ships with a generic `python3` kernelspec; selecting the
registered kernel points it at the environment that has `boto3` + Jupyter.

### Using it

```python
chat("Run QC on the PBMC dataset with a 5% mitochondrial threshold and show me the plots")
```

- Text renders as Markdown; `![](url)` plots render as inline images.
- Each tool the agent calls prints live while compute runs.
- The notebook threads conversation context **client-side** (it sends a short
  transcript with each turn), so follow-up questions keep context. Call
  `reset_chat()` to start a fresh conversation. For server-side memory across
  sessions, enable AgentCore Memory on the Runtime (set `AGENTCORE_MEMORY_ID`);
  the agent already supports it.

The agent ARN is read automatically from SSM
(`/agents/scverse_research_agent_arn`).

**How it works**

```
notebook  →  AgentCore Runtime  →  Gateway (MCP)  →  compute  →  S3
```

Same backend as Open WebUI — a different surface for a different audience.
