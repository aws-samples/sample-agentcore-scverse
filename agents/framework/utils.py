"""
Common utilities for agent framework

Provides reusable functions for configuration, observability, gateway connection,
and error handling shared across all agents.
"""

import os
import logging
import boto3
import json
import requests
import time
import threading
from typing import List, Dict, Any, Tuple, Optional

from aws_lambda_powertools import Logger, Tracer, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp.mcp_client import MCPClient

# Module-level cache for gateway connections
_gateway_cache = {
    'client': None, 'config': None, 'tools': None,
    'tools_ttl': 0, 'token': None, 'token_expiry': 0
}
_gateway_lock = threading.Lock()

_scverse_gateway_cache = {
    'client': None, 'config': None, 'tools': None,
    'tools_ttl': 0, 'token': None, 'token_expiry': 0
}
_scverse_gateway_lock = threading.Lock()

TOOLS_CACHE_TTL = int(os.getenv('TOOLS_CACHE_TTL', '300'))

_boto3_clients = {}
_boto3_lock = threading.Lock()

try:
    from bedrock_agentcore.observability import (
        AgentCoreTracer, AgentCoreMetrics, AgentCoreLogger
    )
except ImportError:
    class MockObservability:
        def trace(self, name, **kwargs):
            from contextlib import contextmanager
            @contextmanager
            def mock_context():
                yield
            return mock_context()
        def error(self, msg, **kwargs):
            logging.error(msg)
    AgentCoreTracer = MockObservability
    AgentCoreMetrics = MockObservability
    AgentCoreLogger = MockObservability

log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, log_level, logging.INFO))


def setup_observability(agent_name: str) -> Tuple[Any, Any, Any, Any, Any, Any]:
    """Set up observability components for an agent."""
    tracer = Tracer()
    metrics = Metrics(namespace=f"{agent_name.replace('_', '').replace('-', '')}")
    observability_logger = Logger()
    agentcore_tracer = AgentCoreTracer()
    agentcore_metrics = AgentCoreMetrics()
    agentcore_logger = AgentCoreLogger()
    logger.info(f"Observability setup completed for agent: {agent_name}")
    return tracer, metrics, observability_logger, agentcore_tracer, agentcore_metrics, agentcore_logger


def _get_cached_boto3_client(service_name: str, region: str = None):
    """Get or create a cached boto3 client."""
    global _boto3_clients
    if region is None:
        region = os.environ.get('AWS_REGION', 'us-west-2')
    cache_key = f"{service_name}_{region}"
    if cache_key not in _boto3_clients:
        with _boto3_lock:
            if cache_key not in _boto3_clients:
                _boto3_clients[cache_key] = boto3.client(
                    service_name, region_name=region,
                    config=boto3.session.Config(connect_timeout=120, read_timeout=900))
    return _boto3_clients[cache_key]


def _get_gateway_config_from_env(prefix: str) -> Optional[Dict[str, Any]]:
    """Generic helper to load gateway config from AGENTCORE_{PREFIX}_* env vars."""
    gateway_url = os.environ.get(f'AGENTCORE_{prefix}_GATEWAY_URL')
    user_pool_id = os.environ.get(f'AGENTCORE_{prefix}_GATEWAY_USER_POOL_ID')
    client_id = os.environ.get(f'AGENTCORE_{prefix}_GATEWAY_CLIENT_ID')
    if not (gateway_url and user_pool_id and client_id):
        return None
    return {
        'gateway_url': gateway_url,
        'gateway_id': os.environ.get(f'AGENTCORE_{prefix}_GATEWAY_ID', ''),
        'user_pool_id': user_pool_id,
        'client_id': client_id,
        'auth_url': os.environ.get(f'AGENTCORE_{prefix}_GATEWAY_AUTH_URL', ''),
        'resource_server_id': os.environ.get(f'AGENTCORE_{prefix}_GATEWAY_RESOURCE_SERVER_ID', ''),
        'oauth_domain': os.environ.get(f'AGENTCORE_{prefix}_GATEWAY_OAUTH_DOMAIN', ''),
    }


def _get_gateway_token(gateway_config: Dict[str, Any]) -> str:
    """Get M2M access token for a gateway via Cognito client credentials."""
    user_pool_id = gateway_config['user_pool_id']
    client_id = gateway_config['client_id']
    region = user_pool_id.split('_')[0] if '_' in user_pool_id else os.environ.get('AWS_REGION', 'us-west-2')

    cognito_client = _get_cached_boto3_client('cognito-idp', region)
    client_response = cognito_client.describe_user_pool_client(UserPoolId=user_pool_id, ClientId=client_id)
    client_secret = client_response['UserPoolClient'].get('ClientSecret')
    if not client_secret:
        raise ValueError("Gateway client secret not found")

    oauth_domain = gateway_config.get('oauth_domain', '')
    token_url = f"https://{oauth_domain}.auth.{region}.amazoncognito.com/oauth2/token"
    resource_server_id = gateway_config.get('resource_server_id', '')
    scope_string = f"{resource_server_id}/gateway:read {resource_server_id}/gateway:write"

    response = requests.post(
        token_url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "client_id": client_id,
              "client_secret": client_secret, "scope": scope_string},
        timeout=10)
    response.raise_for_status()
    return response.json().get("access_token")


# ============================================================
# Database Gateway (Biomni tools)
# ============================================================

def get_database_gateway_config() -> Dict[str, Any]:
    """Retrieve Database Gateway configuration."""
    global _gateway_cache
    if _gateway_cache['config'] is not None:
        return _gateway_cache['config']

    # Try env vars first
    config = _get_gateway_config_from_env('')
    if config:
        # Remap: env vars use AGENTCORE_GATEWAY_* (no prefix)
        pass
    else:
        # Direct env vars pattern
        gateway_url = os.environ.get('AGENTCORE_GATEWAY_URL')
        user_pool_id = os.environ.get('AGENTCORE_GATEWAY_USER_POOL_ID')
        client_id = os.environ.get('AGENTCORE_GATEWAY_CLIENT_ID')
        if gateway_url and user_pool_id and client_id:
            config = {
                'gateway_url': gateway_url,
                'gateway_id': os.environ.get('AGENTCORE_GATEWAY_ID', ''),
                'user_pool_id': user_pool_id,
                'client_id': client_id,
                'auth_url': os.environ.get('AGENTCORE_GATEWAY_AUTH_URL', ''),
                'resource_server_id': os.environ.get('AGENTCORE_GATEWAY_RESOURCE_SERVER_ID', 'database-agentcore-gateway-id'),
                'oauth_domain': os.environ.get('AGENTCORE_GATEWAY_OAUTH_DOMAIN', ''),
            }
        else:
            # SSM fallback
            ssm_client = _get_cached_boto3_client('ssm')
            parameter_name = os.environ.get('DATABASE_GATEWAY_PARAMETER_NAME', 'LambdaMcpServer-Database-Gateway')
            response = ssm_client.get_parameter(Name=parameter_name)
            config = json.loads(response['Parameter']['Value'])

    _gateway_cache['config'] = config
    logger.info("Database Gateway configuration loaded and cached")
    return config


def get_database_gateway_token(gateway_config: Dict[str, Any]) -> str:
    """Get M2M access token for Database Gateway with caching."""
    global _gateway_cache
    current_time = time.time()
    if _gateway_cache['token'] and _gateway_cache['token_expiry'] > current_time:
        return _gateway_cache['token']
    token = _get_gateway_token(gateway_config)
    _gateway_cache['token'] = token
    _gateway_cache['token_expiry'] = current_time + 3300  # ~55 min
    logger.info("Database Gateway OAuth token obtained and cached")
    return token


def create_gateway_client() -> tuple:
    """Create database gateway MCP client."""
    global _gateway_cache
    if _gateway_cache['client'] is not None and _gateway_cache['config'] is not None:
        return _gateway_cache['client'], _gateway_cache['config']
    with _gateway_lock:
        if _gateway_cache['client'] is None:
            config = get_database_gateway_config()
            token = get_database_gateway_token(config)
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            mcp_client = MCPClient(lambda: streamablehttp_client(url=config['gateway_url'], headers=headers))
            _gateway_cache['client'] = mcp_client
            _gateway_cache['config'] = config
            logger.info("Database Gateway client created and cached")
            return mcp_client, config
        return _gateway_cache['client'], _gateway_cache['config']


def get_gateway_tools_with_client(mcp_client) -> List[Any]:
    """Get tools from database gateway with TTL-based caching and pagination."""
    global _gateway_cache
    current_time = time.time()
    if _gateway_cache['tools'] is not None and _gateway_cache['tools_ttl'] > current_time:
        return _gateway_cache['tools']

    gateway_tools = []
    more_tools = True
    pagination_token = None
    while more_tools:
        tmp_tools = mcp_client.list_tools_sync(pagination_token=pagination_token)
        if hasattr(tmp_tools, '__iter__') and not isinstance(tmp_tools, str):
            gateway_tools.extend(tmp_tools)
        else:
            gateway_tools.append(tmp_tools)
        if hasattr(tmp_tools, 'pagination_token') and tmp_tools.pagination_token:
            pagination_token = tmp_tools.pagination_token
        else:
            more_tools = False

    _gateway_cache['tools'] = gateway_tools
    _gateway_cache['tools_ttl'] = current_time + TOOLS_CACHE_TTL
    logger.info(f"Found {len(gateway_tools)} tools from Database Gateway (cached for {TOOLS_CACHE_TTL}s)")
    return gateway_tools


# ============================================================
# scverse Gateway
# ============================================================

def get_scverse_gateway_config() -> Optional[Dict[str, Any]]:
    """Retrieve scverse Gateway configuration from environment variables."""
    global _scverse_gateway_cache
    if _scverse_gateway_cache['config'] is not None:
        return _scverse_gateway_cache['config']
    config = _get_gateway_config_from_env('SCVERSE')
    if not config:
        logger.info("scverse Gateway env vars not set — skipping")
        return None
    if not config['resource_server_id']:
        config['resource_server_id'] = 'scverse-agentcore-gateway-id'
    _scverse_gateway_cache['config'] = config
    logger.info("scverse Gateway configuration loaded")
    return config


def get_scverse_gateway_token(gateway_config: Dict[str, Any]) -> str:
    """Get M2M access token for scverse Gateway with caching."""
    global _scverse_gateway_cache
    current_time = time.time()
    if _scverse_gateway_cache['token'] and _scverse_gateway_cache['token_expiry'] > current_time:
        return _scverse_gateway_cache['token']
    token = _get_gateway_token(gateway_config)
    _scverse_gateway_cache['token'] = token
    _scverse_gateway_cache['token_expiry'] = current_time + 3300
    logger.info("scverse Gateway OAuth token obtained and cached")
    return token


def create_scverse_gateway_client() -> Optional[tuple]:
    """Create scverse gateway MCP client. Returns (mcp_client, config) or None."""
    global _scverse_gateway_cache
    if _scverse_gateway_cache['client'] is not None and _scverse_gateway_cache['config'] is not None:
        return _scverse_gateway_cache['client'], _scverse_gateway_cache['config']
    with _scverse_gateway_lock:
        if _scverse_gateway_cache['client'] is None:
            config = get_scverse_gateway_config()
            if config is None:
                return None
            token = get_scverse_gateway_token(config)
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            mcp_client = MCPClient(lambda: streamablehttp_client(url=config['gateway_url'], headers=headers))
            _scverse_gateway_cache['client'] = mcp_client
            _scverse_gateway_cache['config'] = config
            logger.info("scverse Gateway client created and cached")
            return mcp_client, config
        return _scverse_gateway_cache['client'], _scverse_gateway_cache['config']


def handle_error(error: Exception, context: str = "") -> str:
    """Handle errors consistently and return user-friendly messages."""
    error_msg = str(error).lower()
    # Log only the error type and call-site context; the error message can
    # contain user data from the failing request.
    logger.error("Error in %s: %s", context, type(error).__name__)
    if "gateway" in error_msg or "connection" in error_msg:
        return "Connectivity issues with external databases. Please try again later."
    elif "timeout" in error_msg:
        return "The request is taking longer than expected. Please try again."
    else:
        return f"Technical difficulties. Error: {str(error)[:100]}..."
