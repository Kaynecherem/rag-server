"""
Rate limiting middleware using Redis sliding window counter.
Configurable per-endpoint limits. Falls back to in-memory if Redis unavailable.
"""

import time
import logging
from collections import defaultdict
from typing import Optional
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger("api.ratelimit")


# Default rate limits: {path_prefix: (max_requests, window_seconds)}
DEFAULT_LIMITS = {
    "/api/v1/policies/upload": (10, 60),       # 10 uploads/min
    "/api/v1/communications/upload": (10, 60),  # 10 uploads/min
    "/api/v1/policies/": (60, 60),              # 60 queries/min
    "/api/v1/communications/query": (60, 60),   # 60 queries/min
    "/api/v1/auth/verify": (20, 60),            # 20 verifications/min
    "/api/v1/auth/test-setup": (5, 60),         # 5 setups/min
    "/widget/": (30, 60),                       # 30 widget requests/min
}


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, redis_client=None, limits: dict = None):
        super().__init__(app)
        self.redis = redis_client
        self.limits = limits or DEFAULT_LIMITS
        # In-memory fallback
        self._memory_store: dict[str, list[float]] = defaultdict(list)

    def _get_limit(self, path: str) -> Optional[tuple[int, int]]:
        """Find the matching rate limit for a path."""
        for prefix, limit in self.limits.items():
            if path.startswith(prefix):
                return limit
        return None

    def _get_client_key(self, request: Request) -> str:
        """Build a rate limit key from client IP + path prefix."""
        client_ip = request.client.host if request.client else "unknown"
        # Include auth token hash to differentiate authenticated users
        auth = request.headers.get("Authorization", "")
        if auth:
            import hashlib
            token_hash = hashlib.md5(auth.encode()).hexdigest()[:8]
            return f"{client_ip}:{token_hash}"
        return client_ip

    async def _check_redis(self, key: str, max_requests: int, window: int) -> tuple[bool, int]:
        """Check rate limit using Redis sliding window."""
        try:
            now = time.time()
            pipe = self.redis.pipeline()
            pipe.zremrangebyscore(key, 0, now - window)
            pipe.zadd(key, {str(now): now})
            pipe.zcard(key)
            pipe.expire(key, window)
            results = await pipe.execute()
            count = results[2]
            return count <= max_requests, max_requests - count
        except Exception as e:
            logger.warning(f"Redis rate limit error, falling back to memory: {e}")
            return self._check_memory(key, max_requests, window)

    def _check_memory(self, key: str, max_requests: int, window: int) -> tuple[bool, int]:
        """Fallback in-memory rate limiter."""
        now = time.time()
        timestamps = self._memory_store[key]
        # Remove expired entries
        self._memory_store[key] = [t for t in timestamps if now - t < window]
        timestamps = self._memory_store[key]

        if len(timestamps) >= max_requests:
            return False, 0

        timestamps.append(now)
        return True, max_requests - len(timestamps)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        limit = self._get_limit(request.url.path)
        if not limit:
            return await call_next(request)

        max_requests, window = limit
        client_key = self._get_client_key(request)
        rate_key = f"rl:{request.url.path.split('/')[3] if len(request.url.path.split('/')) > 3 else 'api'}:{client_key}"

        if self.redis:
            allowed, remaining = await self._check_redis(rate_key, max_requests, window)
        else:
            allowed, remaining = self._check_memory(rate_key, max_requests, window)

        if not allowed:
            logger.warning(
                f"Rate limit exceeded: {request.url.path} by {client_key}",
                extra={"path": request.url.path, "client_ip": client_key},
            )
            return JSONResponse(
                status_code=429,
                content={
                    "error": "Rate limit exceeded. Please try again later.",
                    "retry_after": window,
                },
                headers={
                    "Retry-After": str(window),
                    "X-RateLimit-Limit": str(max_requests),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(max_requests)
        response.headers["X-RateLimit-Remaining"] = str(max(0, remaining))
        return response
