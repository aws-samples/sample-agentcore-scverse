"""
BaseAgent class for the agent framework

Provides standardized agent initialization, request processing, and error handling.
"""

import os
import logging
import time
import uuid
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional

from strands import Agent
from strands.models import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from aws_lambda_powertools.metrics import MetricUnit

from utils import (
    setup_observability,
    create_gateway_client,
    create_scverse_gateway_client,
    get_gateway_tools_with_client,
    handle_error
)

log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S', force=True)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('botocore').setLevel(logging.WARNING)
logging.getLogger('boto3').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """Base class for all agents providing common functionality."""

    def __init__(self, agent_name: str, system_prompt: str):
        self.agent_name = agent_name
        self.system_prompt = system_prompt
        self.config = {
            'bedrock_model_id': os.getenv('BEDROCK_MODEL_ID', 'us.anthropic.claude-haiku-4-5-20251001-v1:0'),
            'aws_region': os.getenv('AWS_REGION', 'us-west-2'),
        }
        (self.tracer, self.metrics, self.observability_logger,
         self.agentcore_tracer, self.agentcore_metrics, self.agentcore_logger) = setup_observability(agent_name)
        self.app = BedrockAgentCoreApp()
        self.model = BedrockModel(
            model_id=self.config['bedrock_model_id'],
            region_name=self.config['aws_region'],
            max_tokens=self.get_model_max_tokens(),
            temperature=self.get_model_temperature(),
            top_p=None)

    @abstractmethod
    def get_system_prompt(self) -> str: ...

    @abstractmethod
    def get_agent_name(self) -> str: ...

    def get_model_max_tokens(self) -> int:
        return 10000

    def get_model_temperature(self) -> float:
        return 0.25

    async def invoke(self, payload: Dict[str, Any], context: Optional[Any] = None, session_manager=None):
        """Process a request through the standardized pipeline.

        Connects to available gateways (scverse is primary, Database Gateway
        is optional) and merges their tools for the agent.
        """
        request_id = str(uuid.uuid4())
        start_time = time.time()
        self.metrics.add_metric(name="AgentInvocation", unit=MetricUnit.Count, value=1)

        with self.agentcore_tracer.trace(f"{self.agent_name}_invocation", request_id=request_id):
            try:
                prompt = payload.get("prompt", "Hello")

                try:
                    all_tools = []
                    open_clients = []  # Track clients we need to close

                    # --- scverse Gateway (primary) ---
                    scverse_result = create_scverse_gateway_client()
                    scverse_client = scverse_result[0] if scverse_result else None
                    if scverse_client:
                        try:
                            scverse_client.__enter__()
                            open_clients.append(scverse_client)
                            raw = scverse_client.list_tools_sync()
                            scverse_tools = list(raw) if hasattr(raw, '__iter__') and not isinstance(raw, str) else [raw]
                            all_tools.extend(scverse_tools)
                            logger.info(f"scverse Gateway: {len(scverse_tools)} tools discovered")
                        except Exception as e:
                            logger.warning(f"scverse Gateway tool discovery failed: {e}")

                    # --- Database Gateway (optional — for Biomni tools) ---
                    try:
                        db_client, db_config = create_gateway_client()
                        db_client.__enter__()
                        open_clients.append(db_client)
                        db_tools = get_gateway_tools_with_client(db_client)
                        logger.info(f"Database Gateway: {len(db_tools)} tools discovered")
                    except Exception as e:
                        db_tools = []
                        logger.info(f"Database Gateway not available (optional): {e}")

                    # Deduplicate tools by name
                    seen_names = set()
                    deduped_tools = []
                    for tool in all_tools + db_tools:
                        tool_name = getattr(tool, 'tool_name', None) or getattr(tool, 'name', str(tool))
                        if tool_name not in seen_names:
                            seen_names.add(tool_name)
                            deduped_tools.append(tool)
                    logger.info(f"Total unique tools: {len(deduped_tools)}")

                    try:
                        agent_kwargs = {
                            "tools": deduped_tools,
                            "system_prompt": self.get_system_prompt(),
                            "model": self.model
                        }
                        if session_manager:
                            agent_kwargs["session_manager"] = session_manager
                        agent = Agent(**agent_kwargs)

                        response = agent.stream_async(prompt)
                        tool_name = None
                        async for event in response:
                            if "current_tool_use" in event and event["current_tool_use"].get("name") != tool_name:
                                tool_name = event["current_tool_use"]["name"]
                                yield f"\n\n🔧 Using tool: {tool_name}\n\n"
                            if "data" in event:
                                tool_name = None
                                yield event["data"]

                        logger.info("Request processed successfully with tools")
                    finally:
                        for client in open_clients:
                            try:
                                client.__exit__(None, None, None)
                            except Exception:
                                pass

                except Exception as gateway_error:
                    logger.warning(f"All gateway connections failed: {gateway_error}")
                    logger.info("Falling back to agent without tools...")
                    fallback_prompt = self.get_system_prompt() + "\n\nNote: External tools are currently unavailable."
                    agent_kwargs = {"tools": [], "system_prompt": fallback_prompt, "model": self.model}
                    if session_manager:
                        agent_kwargs["session_manager"] = session_manager
                    agent = Agent(**agent_kwargs)
                    response = agent.stream_async(prompt)
                    tool_name = None
                    async for event in response:
                        if "current_tool_use" in event and event["current_tool_use"].get("name") != tool_name:
                            tool_name = event["current_tool_use"]["name"]
                            yield f"\n\n🔧 Using tool: {tool_name}\n\n"
                        if "data" in event:
                            tool_name = None
                            yield event["data"]

            except Exception as e:
                # Log only the error type; the message/traceback can contain
                # user prompt data processed by the agent.
                logger.error("Agent invocation failed: %s", type(e).__name__)
