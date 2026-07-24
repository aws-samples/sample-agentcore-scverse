"""
Local test script for the scverse Research Agent.

Tests the agent WITHOUT deploying to AgentCore — runs the Strands agent
locally against your AWS credentials. Useful for validating the system
prompt, tool selection logic, and gateway connectivity before deployment.

Usage:
    # Test with a simple prompt (no gateway needed)
    python test_agent_local.py

    # Test with gateway tools (requires deployed CDK stack)
    python test_agent_local.py --with-tools

    # Test with a custom prompt
    python test_agent_local.py --prompt "What scvi-tools model should I use for CITE-seq data?"

Requirements:
    pip install -r requirements.txt
    AWS credentials configured (for Bedrock model access)
"""

import argparse
import asyncio
import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def _get_scverse_system_prompt() -> str:
    """Standalone system prompt for local testing without agentcore SDK."""
    return """You are a scverse Research Agent specializing in single-cell genomics.
You help scientists with scanpy QC, scvi-tools model selection (scVI, scANVI, totalVI,
PeakVI, MultiVI, DestVI, veloVI), and single-cell analysis workflows.
Model selection: scRNA-seq → scVI; with labels → scANVI; CITE-seq → totalVI;
scATAC-seq → PeakVI; Multiome → MultiVI; Spatial → DestVI; RNA velocity → veloVI."""


def test_without_tools(prompt: str):
    """Test agent with no gateway tools — validates system prompt and model access."""
    try:
        from strands import Agent
        # BedrockModel provides managed access to Claude models through Amazon
        # Bedrock's API (not the Anthropic API directly).
        from strands.models import BedrockModel
    except ImportError:
        print("ERROR: strands-agents not installed.")
        print("Run: pip install -r requirements.txt")
        sys.exit(1)

    region = os.getenv("AWS_REGION", "us-west-2")
    model_id = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

    print(f"Testing agent locally (no tools)")
    print(f"  Region: {region}")
    print(f"  Model: {model_id}")
    print(f"  Prompt: {prompt}\n")

    model = BedrockModel(model_id=model_id, region_name=region, max_tokens=4000, temperature=0.25)

    # Import the system prompt from the agent — use a lightweight approach
    # that doesn't require bedrock_agentcore to be installed
    sys.path.insert(0, os.path.dirname(__file__))
    try:
        from scverse_agent import ScverseResearchAgent
        system_prompt = ScverseResearchAgent().get_system_prompt()
    except ImportError:
        # If agentcore SDK not installed, use the prompt directly
        system_prompt = _get_scverse_system_prompt()

    agent = Agent(tools=[], system_prompt=system_prompt, model=model)

    print("--- Agent Response ---")
    result = agent(prompt)
    print(f"\n--- End Response ---")
    print(f"\nResponse length: {len(str(result))} chars")


def test_with_tools(prompt: str):
    """Test agent with gateway tools — requires deployed CDK stack."""
    # Check for required env vars
    required_vars = ["AGENTCORE_SCVERSE_GATEWAY_URL", "AGENTCORE_SCVERSE_GATEWAY_USER_POOL_ID",
                     "AGENTCORE_SCVERSE_GATEWAY_CLIENT_ID"]
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        print(f"ERROR: Missing environment variables for gateway connection:")
        for v in missing:
            print(f"  {v}")
        print(f"\nSet these from your CDK stack outputs, or run without --with-tools")
        sys.exit(1)

    print("Testing agent with gateway tools...")
    print("(This requires the CDK stack to be deployed)\n")

    # Add framework to path
    framework_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'framework')
    sys.path.insert(0, framework_path)

    from utils import create_scverse_gateway_client, get_gateway_tools_with_client
    from strands import Agent
    from strands.models import BedrockModel

    region = os.getenv("AWS_REGION", "us-west-2")
    model_id = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    model = BedrockModel(model_id=model_id, region_name=region, max_tokens=4000, temperature=0.25)

    sys.path.insert(0, os.path.dirname(__file__))
    from scverse_agent import ScverseResearchAgent
    system_prompt = ScverseResearchAgent().get_system_prompt()

    result = create_scverse_gateway_client()
    if not result:
        print("ERROR: Could not connect to scverse gateway")
        sys.exit(1)

    mcp_client, config = result
    with mcp_client:
        tools = get_gateway_tools_with_client(mcp_client)
        print(f"Discovered {len(tools)} tools from gateway\n")

        agent = Agent(tools=tools, system_prompt=system_prompt, model=model)
        print("--- Agent Response ---")
        response = agent(prompt)
        print(f"\n--- End Response ---")


def test_gateway_tools_directly():
    """Test the gateway Lambda functions directly without the agent."""
    print("Testing gateway tools directly (no agent, no AWS)...\n")

    # Add gateway function to path
    gateway_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                                'gateways', 'scverse', 'function')
    sys.path.insert(0, gateway_path)

    # QC runs live on the Container Lambda (scanpy); the gateway no longer
    # ships a local QC validator to unit-test here.

    # Test run_scvi_analysis model selection
    from run_scvi_analysis import run_scvi_analysis
    print("run_scvi_analysis model validation:")

    result = run_scvi_analysis({"model": "nonexistent"})
    assert result["status"] == "error"
    print("  ✅ Unknown model: rejected")

    result = run_scvi_analysis({"model": "scanvi"})
    assert result["status"] == "error" and "labels_key" in result["error"]
    print("  ✅ scANVI without labels: rejected with hint")

    result = run_scvi_analysis({"model": "totalvi"})
    assert result["status"] == "error" and "protein" in result["error"]
    print("  ✅ totalVI without protein data: rejected with hint")

    result = run_scvi_analysis({"model": "peakvi"})
    assert result["status"] == "success"
    print("  ✅ PeakVI: validated successfully")

    print("\n✅ All local validation tests passed")


def main():
    parser = argparse.ArgumentParser(description='Test scverse agent locally')
    parser.add_argument('--prompt', default="What scvi-tools model should I use for a PBMC scRNA-seq dataset with no cell type labels?",
                        help='Prompt to test')
    parser.add_argument('--with-tools', action='store_true',
                        help='Test with gateway tools (requires deployed CDK stack)')
    parser.add_argument('--tools-only', action='store_true',
                        help='Test gateway tool validation directly (no AWS needed)')
    args = parser.parse_args()

    if args.tools_only:
        test_gateway_tools_directly()
    elif args.with_tools:
        test_with_tools(args.prompt)
    else:
        test_without_tools(args.prompt)


if __name__ == "__main__":
    main()
