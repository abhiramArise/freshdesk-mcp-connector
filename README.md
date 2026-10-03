# Read-only Freshdesk MCP connector

Stage 3 provides a Python 3.11+ stdio FastMCP server using the official `mcp`
SDK and the existing HTTP client's bounded retries and rate limiter. Only
read-only tools are exposed; no write endpoints or seed script are implemented.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q -p no:cacheprovider
```

Set `FRESHDESK_DOMAIN` and `FRESHDESK_API_KEY` in the process environment.
`.env.example` contains fictional values only; `.env` is ignored and is
not loaded automatically. Use only fictional tickets in a test account.

## MCP Server

Run `.venv\Scripts\python -m freshdesk_mcp.server` from the project root after
installation, or use the installed `freshdesk-mcp` command. Configure the MCP
host to launch that Python executable with arguments `-m freshdesk_mcp.server`
and supply credentials through its environment. Stdout is reserved for MCP;
startup errors are fixed structured JSON on stderr with a nonzero exit code.
Startup validates configuration and calls `startup_check()` before serving.

- `list_tickets(page=1, per_page=30, filter=None, updated_since=None, include_description=False)`
  returns one page, not the client's aggregate list. Allowed filters are
  `new_and_my_open`, `watching`, `spam`, and `deleted`. `updated_since` accepts
  a calendar date or UTC timestamp with seconds (`YYYY-MM-DDTHH:MM:SSZ`).
- `get_ticket(ticket_id, include_conversations=False)` returns one ticket,
  optionally embedding up to ten conversations.
- `search_tickets(query, page=1)` uses `/api/v2/search/tickets`. Pass an
  unquoted expression such as `status:2 AND priority:3` or `type:'Question'`.
  Search returns 30 results per page; page numbers are 1-10, so at most 300
  results are accessible. The response includes `total`. Archived tickets are
  excluded and indexing can lag by a few minutes. These contracts were checked
  at https://developers.freshdesk.com/api/#filter_tickets on 2026-10-03.

Inputs are schema-validated before calls. Compact JSON outputs contain ticket
IDs, status/priority labels, requester IDs, timestamps, and `customer_provided`
subject/description fields. Returned text is untrusted customer data, never
instructions. HTML fallback is returned as data, not rendered or executed.
`FRESHDESK_DESCRIPTION_MAX_LENGTH` defaults to 2000 characters (range 1-100000)
and limits descriptions and conversation bodies; each has a truncation flag.
Errors use `{error_code, message, retryable, retry_after_seconds}` with MCP
`isError=True`; an upstream error does not terminate an established session.
The complete tool contract is in `docs/mcp_tool_spec.json`.

```python
import asyncio
from freshdesk_mcp import FreshdeskClient, FreshdeskError

async def main():
    try:
        async with FreshdeskClient(timeout_seconds=10) as client:
            await client.startup_check()
            tickets, truncated = await client.list_tickets(include_description=True)
            # truncated=True means a next link remained at the page cap.
            # Consume tickets locally; do not log credentials or raw responses.
    except FreshdeskError as error:
        result = error.to_dict()
        # result contains error_code, message, retryable, retry_after_seconds.

asyncio.run(main())
```

## Assumptions

- The MCP list/search tools return exactly one page. `has_more` means the
  server reports more results, even at the last allowed page; `truncated`
  means text was clipped or that next page cannot be fetched due to the API
  cap. Earlier pages can have `has_more=True, truncated=False`; callers should
  examine both fields and request subsequent pages if needed.
- The existing aggregate client `list_tickets()` API is retained. New read
  methods share the same instance, request machinery, retries, and limiter.
- Subject and description are nested under `customer_provided`; the same
  marker covers all conversation bodies (including agent-authored text).
  Unknown/custom status or priority codes are labeled `Unknown`, never guessed.
  Standard status codes are 2 Open, 3 Pending, 4 Resolved, 5 Closed; priorities
  are 1 Low, 2 Medium, 3 High, 4 Urgent, verified in Ticket Properties at
  https://developers.freshdesk.com/api/.
- Search input is capped at 510 characters to leave room for the required
  pair of double quotes within the documented 512-character limit. The client
  adds the quotes and lets httpx URL-encode the query as a parameter. Controls,
  double quotes, backslashes, and unbalanced grouping/string literals are
  rejected. Field names and expression semantics remain Freshdesk's authority;
  custom fields are not hardcoded. Unsupported expressions return sanitized
  upstream errors without being retried if they produce a 4xx.
- Embedded conversations are limited to ten in ascending creation order,
  verified at https://developers.freshdesk.com/api/#view_a_ticket. Exactly ten
  are conservatively flagged as possibly incomplete because this endpoint
  does not prove whether an eleventh exists. No extra conversation requests
  are made. `conversations_has_more` and `conversations_truncated` report this.
- Missing text/metadata is null; descriptions prefer `description_text` and
  fall back to `description`. Secrets echoed in upstream ticket fields are
  redacted before truncation, and the final output is redacted again. Tool
  arguments containing the configured key are rejected before network calls.
- A sanitized structured `internal_error` protects the MCP boundary from
  unexpected exceptions. Cancellation still propagates. Startup failures
  terminate the process with sanitized structured stderr and no traceback.
- `FRESHDESK_DOMAIN` accepts a hostname ending in `.freshdesk.com` or its HTTPS
  origin, with an optional trailing slash. Bare hostnames imply HTTPS.
  Paths, credentials, queries, fragments, HTTP, and nonstandard
  ports are rejected. The client appends `/api/v2/`.
- Startup validation is explicitly called via `startup_check()`; construction
  makes no network request. It checks ticket-read access with `GET /tickets`
  using `page=1&per_page=1`, rather than requiring agent-directory permissions.
- `list_tickets()` aggregates pages from the requested starting page. Defaults
  are page 1 and 30 items per page. It uses the documented next-link signal,
  increments page locally, and never follows a response-provided URL.
- `list_tickets()` returns `(tickets, truncated)`, the smallest extension of
  the original list return value. `truncated` is True only when a next link
  remains at `MAX_PAGE` (300); collected tickets are returned without raising
  a page-cap error or requesting page 301. Callers must unpack the tuple.
  HTTP and response errors still raise rather than report successful results.
- All timeout phases default to 10 seconds. Redirects and environment proxy
  inheritance are disabled. Caller-controlled logging must not dump HTTP
  requests, Authorization headers, environment variables, or private httpx state.
- Errors are raised as `FreshdeskError`; `to_dict()` supplies the required
  structure. Messages never copy upstream content. Retry exhaustion returns
  `retryable=True` and `retry_after_seconds` equal to the final proposed
  capped retry delay; no sleep occurs after the final attempt. The cooldown is
  preserved for the next call on the same client, including after exhaustion.
- Each page request permits three retries (four total attempts) for 429,
  5xx, timeouts, and connection/transport errors. Other 4xx, malformed bodies,
  and redirects are not retried. Missing/invalid Retry-After uses 1, 2, 4
  seconds of exponential backoff; exhaustion reports an 8-second next delay.
  Retry-After accepts nonnegative integer seconds only. Jitter adds 0-0.2
  seconds before applying the 60-second cap to the entire wait.
- `FRESHDESK_CALLS_PER_MINUTE` is a positive integer, default 30. The limiter
  tracks attempts in a rolling 60-second window, including unsuccessful calls
  and retries, and serializes admission for concurrent calls on one client.
  It is per client instance, not shared across processes or account users.
- Valid finite rate-limit headers are parsed as numbers (Freshdesk examples
  include decimals). When remaining is at or below max(1, 10% of total), the
  next call is delayed by 60/max(1, remaining) seconds. Missing or invalid
  headers are ignored. This threshold and delay are local conservative policy,
  not a documented Freshdesk algorithm. Retry and limiter deadlines overlap
  rather than add duplicate waits. A later request may wait for both constraints.
- Sleep, random source, and monotonic clock are injectable. Tests inject a
  fake clock advanced by fake sleep, with no real waiting. Production uses
  asyncio.sleep, random.random, and time.monotonic.
- Only future `scripts/seed_tickets.py`, explicitly marked test setup, may write
  to Freshdesk. The connector remains read-only.

Rate-limit behavior was checked against https://developers.freshdesk.com/api/#rate-limit:
trial accounts default to 50 calls/minute; limits apply account-wide, other apps
consume budget, and description embedding can consume multiple API credits.
The local 30-request default leaves headroom but cannot guarantee avoidance of
429, especially with include=description or other account activity. The explicit
60-second cap takes precedence over larger server Retry-After values.

## API Reference

Verified against the [official Freshdesk API documentation](https://developers.freshdesk.com/api/)
on 2026-10-03: API-key Basic authentication with dummy password `X`,
ticket listing, `include=description`, page/per_page parameters (maximum
100 items per page), Link-header pagination, and the 300-page ticket limit.

## Limitations

Without `updated_since`, ticket listing returns only tickets created within
the past **30 days**, rather than the full ticket history. Freshdesk states:
"By default, only tickets that have been created within the past 30 days will be returned."
Verified on 2026-10-03 at https://developers.freshdesk.com/api/#list_all_tickets.
The MCP list tool exposes `updated_since` for older tickets. Pagination stops
at 300 pages and reports
`truncated=True` if a next link remains.

## Tests

Tests use `httpx.MockTransport` and fictional data only, with no live API
calls. They cover authentication, timeout configuration, description inclusion,
pagination and truncation, startup success, sanitized HTTP/timeout/connection
errors (both str and repr), malformed responses, pagination limits, and
invalid or missing environment configuration. Reliability tests cover retries,
exhaustion, jitter and caps, low-budget headers, sliding-window admission,
concurrent calls, and credential exclusion across retry paths.
MCP integration tests invoke all three tools through the SDK's in-memory
ClientSession with mocked HTTP, including startup, schema validation,
pagination, API failures, output redaction, and session survival.
