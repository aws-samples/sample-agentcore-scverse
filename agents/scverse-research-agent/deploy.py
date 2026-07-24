"""
Deployment script for scverse Research Agent
"""

import os
import sys
import argparse
import shutil
import boto3
from dotenv import load_dotenv

framework_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'framework')
sys.path.insert(0, framework_path)

from deploy_utils import deploy_agent, cleanup_docker_files, setup_dockerignore

load_dotenv()


def main():
    os.environ['AWS_REGION'] = os.getenv('AWS_REGION', 'us-west-2')
    os.environ['AWS_DEFAULT_REGION'] = os.environ['AWS_REGION']

    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    parser = argparse.ArgumentParser(description='Deploy scverse Research Agent to AgentCore')
    parser.add_argument('--agent_name', default='scverse_research_agent')
    parser.add_argument('--execution_role', help='IAM execution role ARN (created if omitted)')
    parser.add_argument('--gateway_id', help='Gateway ID (optional)')
    args = parser.parse_args()

    # Copy framework for Docker build context
    local_framework = os.path.join(script_dir, 'framework')
    if os.path.exists(local_framework):
        shutil.rmtree(local_framework)
    shutil.copytree(framework_path, local_framework)

    cleanup_docker_files(script_dir)
    setup_dockerignore(script_dir, ["scverse_agent.py", "requirements.txt", "framework/"])

    env_overrides = {
        "BEDROCK_MODEL_ID": os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
        "AWS_REGION": os.environ['AWS_REGION'],
        "AGENTCORE_MEMORY_ID": os.getenv("AGENTCORE_MEMORY_ID", ""),
        "PYTHONUNBUFFERED": "1",
        "LOG_LEVEL": "INFO",
    }

    # Pass Gateway env vars if set (from CDK outputs or environment)
    gateway_vars = [
        "AGENTCORE_SCVERSE_GATEWAY_URL",
        "AGENTCORE_SCVERSE_GATEWAY_USER_POOL_ID",
        "AGENTCORE_SCVERSE_GATEWAY_CLIENT_ID",
        "AGENTCORE_SCVERSE_GATEWAY_OAUTH_DOMAIN",
        "AGENTCORE_SCVERSE_GATEWAY_RESOURCE_SERVER_ID",
        "AGENTCORE_SCVERSE_GATEWAY_ID",
    ]
    for var in gateway_vars:
        val = os.getenv(var, "")
        if val:
            env_overrides[var] = val

    try:
        agent_arn = deploy_agent(
            agent_name=args.agent_name,
            execution_role_arn=args.execution_role,
            gateway_id=args.gateway_id,
            entrypoint='scverse_agent.py',
            requirements_file='requirements.txt',
            env_overrides=env_overrides,
        )
        print(f"\nDeployment complete. Agent ARN: {agent_arn}")

        # Store ARN in SSM for discovery
        try:
            ssm = boto3.client('ssm', region_name=os.environ['AWS_REGION'])
            ssm.put_parameter(
                Name=f"/agents/{args.agent_name}_arn",
                Value=agent_arn, Type='String', Overwrite=True)
            print(f"Agent ARN stored in SSM: /agents/{args.agent_name}_arn")
        except Exception as e:
            print(f"Warning: Could not store ARN in SSM: {e}")

        return agent_arn
    except Exception as e:
        print(f"Deployment failed: {e}")
        return None
    finally:
        if os.path.exists(local_framework):
            shutil.rmtree(local_framework)


if __name__ == "__main__":
    result = main()
    exit(0 if result else 1)
