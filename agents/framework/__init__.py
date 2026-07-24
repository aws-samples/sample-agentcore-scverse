"""
Agent Framework

A reusable framework for creating AgentCore agents with common patterns
for configuration, observability, gateway integration, and deployment.
"""

from .base_agent import BaseAgent
from .utils import setup_observability, get_gateway_tools_with_client, handle_error

__version__ = "1.0.0"
__all__ = ["BaseAgent", "setup_observability", "get_gateway_tools_with_client", "handle_error"]
