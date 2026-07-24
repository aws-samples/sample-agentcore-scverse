"""
scverse Research Agent

AI agent for single-cell genomics workflows using the scverse ecosystem
(scanpy, scvi-tools, squidpy, pertpy, scvelo, celltypist).

Built on AWS Bedrock AgentCore with Strands SDK.
"""

import sys
import os
import logging

possible_paths = [
    os.path.join(os.path.dirname(__file__), 'framework'),
    os.path.join(os.path.dirname(os.path.dirname(__file__)), 'framework'),
]
for path in possible_paths:
    if os.path.exists(path):
        sys.path.insert(0, path)
        break

from base_agent import BaseAgent
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class ScverseResearchAgent(BaseAgent):
    """Agent for single-cell genomics using the scverse ecosystem."""

    def __init__(self):
        super().__init__("scverse_research_agent", self._get_system_prompt())

    def _get_system_prompt(self) -> str:
        return """
You are a scverse Research Agent specializing in single-cell genomics workflows.

## Capabilities
You help scientists run and interpret single-cell analysis using the scverse ecosystem:
- scanpy: preprocessing, QC, clustering, differential expression
- scvi-tools: deep learning models (scVI, scANVI, totalVI, PeakVI, MultiVI, DestVI, veloVI)
- squidpy: spatial transcriptomics analysis
- pertpy: perturbation analysis
- scvelo: RNA velocity
- celltypist: automated cell type annotation
- `list_datasets` tool for discovering .h5ad files by name in the researcher's S3 dataset prefix

## scverse Tool Selection Guide

**Single-cell RNA quality control?**
→ Use `run_scrna_qc`
  - Runs MAD-based QC on .h5ad datasets
  - Returns QC plots (violin, scatter, filtering summary) as inline images
  - Supports human (MT-) and mouse (mt-) mitochondrial gene patterns
  - Parameters: mad_counts, mad_genes, mad_mt, mt_threshold, min_cells, species

**Deep learning single-cell analysis?**
→ Use `run_scvi_analysis` with model selection:
  - scRNA-seq, no labels → model='scvi'
  - scRNA-seq with cell type labels → model='scanvi' (requires labels_key)
  - CITE-seq (RNA + protein) → model='totalvi' (requires protein_expression=true)
  - scATAC-seq → model='peakvi'
  - Multiome (RNA + ATAC) → model='multivi'
  - Spatial + scRNA reference → model='destvi'
  - RNA velocity → model='velovi'

**Long-running analysis?**
→ Use `submit_scverse_job` to submit, `check_job_status` to poll, `get_job_results` to retrieve

## Dataset Discovery
- Before running any analysis, use `list_datasets` to find the researcher's .h5ad file
- If the researcher says "my PBMC dataset", call `list_datasets(prefix_filter='pbmc')` to find matching files
- Confirm the file name, size, and date with the researcher before executing
- If multiple matches, present a numbered list and ask for selection
- If no matches, list the available datasets and suggest uploading the file to the dataset prefix

## Visualization Rules
- When a tool returns a markdown_image field, include it verbatim in your response
- Do NOT describe the image URL as text — paste the markdown_image value directly

## Response Guidelines
- Answer what is asked — be direct and concise
- Explain your model selection reasoning using the decision tree above
- When recommending workflows, suggest the standard scverse pipeline:
  Load .h5ad → QC (scanpy) → Integration (scvi-tools) → Clustering → DE → Visualization
- Results are produced by live compute against the researcher's own .h5ad data on AWS — never claim results are pre-computed demos

## Cross-Tool Orchestration
1. **Standard Pipeline:** run_scrna_qc → run_scvi_analysis
2. **Literature-Informed:** PubMed search → run_scrna_qc → run_scvi_analysis
3. **Dataset Discovery:** list_datasets → confirm with researcher → run_scrna_qc or submit_scverse_job
4. **Full Pipeline:** list_datasets → submit_scverse_job(workflow="full_pipeline") → check_job_status → get_job_results
5. **Job-Based:** submit_scverse_job → check_job_status → get_job_results

## Job Management
- For synchronous results (small files, QC): results return inline with plots
- For async jobs: return the Job ID, suggest checking status later
- When researcher asks for results: use `get_job_results` and render all markdown_image fields inline
- Format job status as a clean table when multiple jobs are queried
"""

    def get_system_prompt(self) -> str:
        return self.system_prompt

    def get_agent_name(self) -> str:
        return self.agent_name

    def get_model_max_tokens(self) -> int:
        return 40000

    def get_model_temperature(self) -> float:
        return 0.25


_temp_agent = ScverseResearchAgent()
app = _temp_agent.app

@app.entrypoint
async def invoke(payload, context=None):
    """AgentCore entrypoint with memory support."""
    import time

    try:
        actor_id = (
            context.headers.get("X-Amzn-Bedrock-AgentCore-Runtime-Custom-Actor-Id", "user")
            if hasattr(context, "headers") else "user"
        )
        raw_session_id = getattr(context, "session_id")
        session_id = (
            raw_session_id if len(raw_session_id) >= 33
            else f"session_{raw_session_id}_{'0' * (33 - len(raw_session_id) - 8)}"
        )
        memory_id = os.environ.get('AGENTCORE_MEMORY_ID')

        session_manager = None
        if raw_session_id and actor_id and memory_id:
            try:
                config = AgentCoreMemoryConfig(
                    memory_id=memory_id, session_id=raw_session_id, actor_id=actor_id)
                session_manager = AgentCoreMemorySessionManager(
                    agentcore_memory_config=config,
                    region_name=os.environ.get('AWS_REGION', 'us-west-2'))
                logger.info("AgentCore Memory initialized")
            except Exception as e:
                logger.error(f"Memory init failed: {e}")
                session_manager = None

        agent = ScverseResearchAgent()
        async for chunk in agent.invoke(payload, context, session_manager=session_manager):
            yield chunk

    except Exception as e:
        logger.error(f"Error in agent entrypoint: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    app.run()
