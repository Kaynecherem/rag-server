# Hardening Integration Guide

## What's Included

```
app/
├── middleware/
│   ├── __init__.py          # Package exports
│   ├── request_id.py        # Assigns X-Request-ID to every request
│   ├── logging_mw.py        # Logs every request with method, path, status, timing
│   ├── rate_limit.py        # Per-endpoint rate limiting (Redis or in-memory fallback)
│   └── error_handler.py     # Global exception handlers (never leaks stack traces)
├── utils/
│   ├── __init__.py          # Package exports
│   ├── logging.py           # Structured JSON logging with request context
│   └── retry.py             # Retry decorator with exponential backoff + jitter
└── main_updated.py          # Updated main.py wiring everything together
```

## How to Apply

### Step 1: Copy new directories

```powershell
# From your project root
xcopy /E hardening\app\middleware app\middleware\
xcopy /E hardening\app\utils app\utils\
```

### Step 2: Update main.py

Replace your current `app/main.py` with `hardening/app/main_updated.py`:

```powershell
copy hardening\app\main_updated.py app\main.py
```

Or manually merge — the key additions are:
1. `setup_logging()` call before app creation
2. Three middleware additions (RequestID, Logging, RateLimit)
3. `register_exception_handlers(app)` call
4. `/health/deep` endpoint
5. Docs hidden in production (`docs_url=None` when not debug)

### Step 3: Add retry decorators to services

See `RETRY_PATCHES.py` for exact locations. In short:

```python
# Top of each service file
from app.utils.retry import retry_async

# On each external API call
@retry_async(max_retries=3, base_delay=1.0)
async def get_embeddings(self, texts):
    ...
```

### Step 4: Deploy

```powershell
# Upload to EC2
scp -i ~/.ssh/insurance-rag2.pem -r app/middleware app/utils ec2-user@18.211.76.143:/opt/insurance-rag/app/
scp -i ~/.ssh/insurance-rag2.pem app/main.py ec2-user@18.211.76.143:/opt/insurance-rag/app/

# SSH in and rebuild
ssh -i ~/.ssh/insurance-rag2.pem ec2-user@18.211.76.143
cd /opt/insurance-rag
sudo docker compose down
sudo docker compose up -d --build
```

## What Each Component Does

### Structured Logging (utils/logging.py)
- All logs output as JSON: `{"timestamp":"...","level":"INFO","message":"...","request_id":"abc123"}`
- Request ID and tenant ID automatically included via context vars
- Noisy libraries (httpx, openai, pinecone) silenced to WARNING level

### Request ID (middleware/request_id.py)
- Every request gets a unique 8-char ID
- Returned in `X-Request-ID` response header
- Included in all log lines for tracing
- Client can send their own via `X-Request-ID` header

### Request Logging (middleware/logging_mw.py)
- Logs: `POST /api/v1/policies/POL-001/query → 200 (342ms)`
- Includes client IP, method, path, status, duration
- Skips /health to reduce noise
- WARNING level for 4xx/5xx responses

### Rate Limiting (middleware/rate_limit.py)
- Per-endpoint limits (configurable)
- Uses Redis sliding window (falls back to in-memory)
- Returns 429 with Retry-After header
- Rate limit headers on every response: X-RateLimit-Limit, X-RateLimit-Remaining
- Defaults:
  - Uploads: 10/min
  - Queries: 60/min
  - Auth verification: 20/min
  - Widget: 30/min

### Error Handler (middleware/error_handler.py)
- Catches ALL unhandled exceptions
- Returns generic "internal error" to client (no stack traces)
- Logs full exception with stack trace server-side
- Consistent JSON error format: `{"error": "message"}`
- Handles validation errors with readable messages

### Retry Logic (utils/retry.py)
- Exponential backoff: 1s → 2s → 4s (configurable)
- Random jitter to prevent thundering herd
- Recognizes retryable errors from OpenAI, Anthropic, Pinecone
- Retries on 429, 500, 502, 503, 504
- Logs each retry attempt with delay

### Deep Health Check (/health/deep)
- Checks: PostgreSQL, Redis, Pinecone connectivity
- Verifies API keys are configured
- Returns 503 if any dependency is down
- Use for monitoring dashboards, not load balancer probes
