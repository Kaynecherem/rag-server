"""
Retry logic with exponential backoff for external API calls.
Handles transient failures from OpenAI, Anthropic, and Pinecone.
"""

import asyncio
import functools
import logging
import time
from typing import Callable, Sequence, Type

logger = logging.getLogger(__name__)

# Default retryable exceptions by provider
OPENAI_RETRYABLE = (
    "RateLimitError",
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
)

ANTHROPIC_RETRYABLE = (
    "RateLimitError",
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
    "OverloadedError",
)

PINECONE_RETRYABLE = (
    "ServiceException",
    "PineconeException",
)


def retry_async(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    retryable_exceptions: tuple = (Exception,),
    retryable_status_codes: tuple = (429, 500, 502, 503, 504),
):
    """
    Async retry decorator with exponential backoff.

    Usage:
        @retry_async(max_retries=3, retryable_exceptions=(openai.RateLimitError,))
        async def call_openai(...):
            ...
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e

                    # Check if exception is retryable
                    is_retryable = False

                    # Check by exception type
                    if isinstance(e, retryable_exceptions):
                        is_retryable = True

                    # Check by exception class name (avoids import issues)
                    exc_name = type(e).__name__
                    if exc_name in OPENAI_RETRYABLE + ANTHROPIC_RETRYABLE + PINECONE_RETRYABLE:
                        is_retryable = True

                    # Check by status code if available
                    status = getattr(e, "status_code", None) or getattr(e, "status", None)
                    if status and status in retryable_status_codes:
                        is_retryable = True

                    if not is_retryable or attempt == max_retries:
                        logger.error(
                            "API call failed (no more retries)",
                            extra={
                                "error_type": exc_name,
                                "method": func.__name__,
                                "attempt": attempt + 1,
                            },
                        )
                        raise

                    # Calculate delay with exponential backoff + jitter
                    delay = min(base_delay * (backoff_factor ** attempt), max_delay)
                    # Add 10-25% jitter to prevent thundering herd
                    import random
                    jitter = delay * random.uniform(0.1, 0.25)
                    delay += jitter

                    logger.warning(
                        f"API call failed, retrying in {delay:.1f}s "
                        f"(attempt {attempt + 1}/{max_retries + 1}): {exc_name}: {e}",
                        extra={
                            "error_type": exc_name,
                            "method": func.__name__,
                            "attempt": attempt + 1,
                            "retry_delay": round(delay, 1),
                        },
                    )

                    await asyncio.sleep(delay)

            raise last_exception

        return wrapper
    return decorator


def retry_sync(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    retryable_exceptions: tuple = (Exception,),
):
    """Synchronous version of retry decorator."""
    def decorator(func: Callable):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exception = e

                    if attempt == max_retries:
                        raise

                    delay = min(base_delay * (backoff_factor ** attempt), max_delay)
                    import random
                    delay += delay * random.uniform(0.1, 0.25)

                    logger.warning(
                        f"Call failed, retrying in {delay:.1f}s "
                        f"(attempt {attempt + 1}/{max_retries + 1}): {e}"
                    )
                    time.sleep(delay)

            raise last_exception

        return wrapper
    return decorator
