"""
Retry utilities for agent framework

Provides reusable retry mechanisms with exponential backoff and AWS waiter wrappers.
"""

import time
import logging
from typing import Callable, Any, Optional
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    RetryError
)
import boto3
from botocore.exceptions import ClientError, WaiterError

logger = logging.getLogger(__name__)


class RetryHandler:
    """Configurable retry mechanism with exponential backoff"""

    @staticmethod
    def retry_with_backoff(func: Callable, max_attempts: int = 3,
                           min_wait: int = 1, max_wait: int = 10,
                           multiplier: int = 2, *args, **kwargs) -> Any:
        @retry(stop=stop_after_attempt(max_attempts),
               wait=wait_exponential(multiplier=multiplier, min=min_wait, max=max_wait),
               reraise=True)
        def _execute():
            return func(*args, **kwargs)
        try:
            return _execute()
        except RetryError as e:
            logger.error(f"All retry attempts failed for {func.__name__}: {e}")
            raise

    @staticmethod
    def wait_for_resource(check_func: Callable[[], bool], timeout: int = 60,
                          interval: int = 5, resource_name: str = "resource") -> bool:
        start_time = time.time()
        elapsed = 0
        while elapsed < timeout:
            try:
                if check_func():
                    logger.info(f"{resource_name} is ready after {elapsed:.1f}s")
                    return True
            except Exception as e:
                logger.debug(f"Check failed for {resource_name}: {e}")
            time.sleep(interval)
            elapsed = time.time() - start_time
        logger.warning(f"{resource_name} not ready after {timeout}s timeout")
        return False


class AWSWaiterHelper:
    """Helper class for AWS resource waiters"""

    @staticmethod
    def wait_for_iam_role(role_name: str, timeout: int = 60) -> bool:
        iam_client = boto3.client('iam')
        def check_role():
            try:
                iam_client.get_role(RoleName=role_name)
                return True
            except ClientError as e:
                if e.response['Error']['Code'] == 'NoSuchEntity':
                    return False
                raise
        logger.info(f"Waiting for IAM role {role_name} to propagate...")
        return RetryHandler.wait_for_resource(
            check_func=check_role, timeout=timeout, interval=2,
            resource_name=f"IAM role {role_name}")

    @staticmethod
    def wait_for_agentcore_endpoint(runtime, timeout: int = 300,
                                     interval: int = 10) -> Optional[str]:
        end_statuses = ['READY', 'CREATE_FAILED', 'DELETE_FAILED', 'UPDATE_FAILED']
        start_time = time.time()
        logger.info("Waiting for AgentCore endpoint to be ready...")
        while (time.time() - start_time) < timeout:
            try:
                status_response = runtime.status()
                status = status_response.endpoint['status']
                logger.info(f"AgentCore endpoint status: {status}")
                if status in end_statuses:
                    return status
                time.sleep(interval)
            except Exception as e:
                logger.error(f"Error checking AgentCore status: {e}")
                return None
        logger.warning(f"AgentCore endpoint not ready after {timeout}s timeout")
        return None
