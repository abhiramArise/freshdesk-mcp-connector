# Read-only Freshdesk client

Stage 1 provides a Python 3.11+ async HTTP client. No MCP tools, retries,
write endpoints, or seed script are implemented. The official `mcp` SDK
is declared as a dependency for later stages.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q
```

Set `FRESHDESK_DOMAIN` and `FRESHDESK_API_KEY` in the process environment.
`.env.example` contains fictional values only; `.env` is ignored and is
not loaded automatically. Use only fictional tickets in a test account.

```python
import asyncio
from freshdesk_mcp import FreshdeskClient, FreshdeskError

async def main():
    try:
        async with FreshdeskClient(timeout_seconds=10) as client:
            await client.startup_check()
            tickets = await client.list_tickets(include_description=True)
            # Consume tickets locally; do not log credentials or raw responses.
    except FreshdeskError as error:
        result = error.to_dict()
        # result contains error_code, message, retryable, retry_after_seconds.

asyncio.run(main())
```

## Assumptions

- `FRESHDESK_DOMAIN` accepts a hostname or HTTPS origin, with an optional
  trailing slash. Paths, credentials, queries, fragments, HTTP, and nonstandard
  ports are rejected. The client appends `/api/v2/`.
- Startup validation is explicitly called via `startup_check()`; construction
  makes no network request. It checks ticket-read access with `GET /tickets`
  using `page=1&per_page=1`, rather than requiring agent-directory permissions.
- `list_tickets()` aggregates pages from the requested starting page. Defaults
  are page 1 and 30 items per page. It uses the documented next-link signal,
  increments page locally, and never follows a response-provided URL.
- Freshdesk's ticket endpoint defaults to tickets created in the last 30 days.
  Older-ticket filters are outside this stage. There is a 300-page API limit;
  a next link at that limit produces an error rather than partial success.
- All timeout phases default to 10 seconds. Redirects and environment proxy
  inheritance are disabled. Caller-controlled logging must not dump HTTP
  requests, Authorization headers, environment variables, or private httpx state.
- Errors are raised as `FreshdeskError`; `to_dict()` supplies the required
  structure. Messages never copy upstream content. `retryable` describes the
  failure, but this stage performs one attempt only and returns
  `retry_after_seconds=None`. Retry-After handling and retries are deferred.
- Only future `scripts/seed_tickets.py`, explicitly marked test setup, may write
  to Freshdesk. The connector remains read-only.

## API Reference

Verified against the [official Freshdesk API documentation](https://developers.freshdesk.com/api/)
on 2026-10-03: API-key Basic authentication with dummy password `X`,
ticket listing, `include=description`, page/per_page parameters (maximum
100 items per page), Link-header pagination, and the 300-page ticket limit.

## Tests

Tests use `httpx.MockTransport` and fictional data only, with no live API
calls. They cover authentication, timeout configuration, description inclusion,
pagination, startup success, sanitized 401/403/404 errors, timeouts, and
invalid or missing environment configuration.
