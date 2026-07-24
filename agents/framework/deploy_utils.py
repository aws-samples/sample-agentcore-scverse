"""
Deployment utilities for agent framework
"""

import os
import json
import time
import boto3
from typing import Dict, Any, List, Optional
from boto3.session import Session
from bedrock_agentcore_starter_toolkit import Runtime
from retry_utils import RetryHandler, AWSWaiterHelper


def create_standard_iam_role(agent_name: str) -> Dict[str, Any]:
    """Create IAM role using standard template."""
    iam_client = boto3.client('iam')
    role_name = f'agentcore-{agent_name}-role'
    boto_session = Session()
    region = boto_session.region_name
    account_id = boto3.client("sts").get_caller_identity()["Account"]

    template_path = os.path.join(os.path.dirname(__file__), 'templates', 'iam_role.json')
    with open(template_path, 'r') as f:
        role_policy_json = f.read()
    role_policy_json = role_policy_json.replace('${region}', region)
    role_policy_json = role_policy_json.replace('${account_id}', account_id)
    role_policy_json = role_policy_json.replace('${agent_name}', agent_name)
    role_policy = json.loads(role_policy_json)

    assume_role_policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "AssumeRolePolicy", "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": account_id},
                "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account_id}:*"}
            }
        }]
    })

    try:
        iam_role = iam_client.create_role(RoleName=role_name, AssumeRolePolicyDocument=assume_role_policy)
        AWSWaiterHelper.wait_for_iam_role(role_name, timeout=30)
    except iam_client.exceptions.EntityAlreadyExistsException:
        policies = iam_client.list_role_policies(RoleName=role_name, MaxItems=100)
        for p in policies['PolicyNames']:
            iam_client.delete_role_policy(RoleName=role_name, PolicyName=p)
        iam_client.delete_role(RoleName=role_name)
        iam_role = iam_client.create_role(RoleName=role_name, AssumeRolePolicyDocument=assume_role_policy)

    iam_client.put_role_policy(PolicyDocument=json.dumps(role_policy), PolicyName="AgentCorePolicy", RoleName=role_name)
    return iam_role


def get_standard_env_vars(agent_name: str, **overrides) -> Dict[str, str]:
    """Get standard environment variables with agent-specific overrides."""
    env_vars = {
        "AWS_DEFAULT_REGION": os.getenv("AWS_REGION", "us-west-2"),
        "AWS_REGION": os.getenv("AWS_REGION", "us-west-2"),
        "AGENT_NAME": agent_name,
        "BEDROCK_MODEL_ID": os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
        "POWERTOOLS_SERVICE_NAME": agent_name,
        "OTEL_SERVICE_NAME": agent_name,
        "OTEL_PROPAGATORS": "tracecontext,baggage,xray",
        "OTEL_PYTHON_DISABLED_INSTRUMENTATIONS": "urllib3,botocore",
    }
    env_vars.update(overrides)
    return env_vars


def deploy_agent(agent_name: str, execution_role_arn: Optional[str] = None,
                 gateway_id: Optional[str] = None, **config) -> str:
    """Deploy an agent using standardized workflow. Returns agent ARN."""
    print(f"Deploying agent: {agent_name}")
    boto_session = Session()
    aws_region = boto_session.region_name

    if not execution_role_arn:
        iam_role = create_standard_iam_role(agent_name)
        execution_role_arn = iam_role['Role']['Arn']
        AWSWaiterHelper.wait_for_iam_role(f'agentcore-{agent_name}-role', timeout=60)

    env_vars = get_standard_env_vars(agent_name, **config.get('env_overrides', {}))
    runtime = Runtime()
    runtime.configure(
        entrypoint=config.get('entrypoint', 'agent.py'),
        execution_role=execution_role_arn,
        auto_create_ecr=True,
        requirements_file=config.get('requirements_file', 'requirements.txt'),
        region=aws_region, agent_name=agent_name)

    try:
        launch_result = runtime.launch(env_vars=env_vars)
        agent_arn = launch_result.agent_arn
    except Exception as e:
        if "already exists" in str(e) or "ConflictException" in str(e):
            launch_result = runtime.launch(env_vars=env_vars, auto_update_on_conflict=True)
            agent_arn = launch_result.agent_arn
        else:
            raise

    final_status = AWSWaiterHelper.wait_for_agentcore_endpoint(runtime, timeout=300, interval=10)
    if final_status != 'READY':
        raise Exception(f"Deployment failed with status: {final_status}")
    print(f"Agent deployed: {agent_arn}")
    return agent_arn


def cleanup_docker_files(script_dir: str):
    for f in [".dockerignore", "Dockerfile", ".bedrock_agentcore.yaml"]:
        path = os.path.join(script_dir, f)
        if os.path.exists(path):
            os.remove(path)


def setup_dockerignore(script_dir: str, files_to_copy: List[str]):
    excludes = ['.venv/', '__pycache__/', '.git/', '.env*', 'deploy.py', '.bedrock_agentcore.yaml', 'README.md']
    with open(os.path.join(script_dir, ".dockerignore"), "a") as f:
        f.write("\n# Auto-generated exclusions\n")
        for item in excludes:
            f.write(item + "\n")
