# Read-only Freshdesk MCP connector

Python 3.11+ stdio FastMCP server using the official `mcp` SDK, pinned to the
locally tested version **1.30.0**, and the HTTP client's retries and rate limiter.
Only read-only tools are exposed. Stage 4 adds standalone test setup and demo
scripts; it does not add connector write methods.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q -p no:cacheprovider
```

For real-account operation, set `FRESHDESK_DOMAIN` and `FRESHDESK_API_KEY` in the process environment.
`.env.example` contains fictional values only; `.env` is ignored and is
not loaded automatically. Use only fictional tickets in a test account.

Run all commands from `D:\Games\freshdesk-mcp-connector`. Obtain the key from
your test account profile and provide it through a secure environment launcher;
do not paste it into shell history, client configuration, or documentation.

| Environment variable | Meaning / default |
| --- | --- |
| `FRESHDESK_DOMAIN` | Required Freshdesk hostname or HTTPS origin; no path/query. |
| `FRESHDESK_API_KEY` | Required secret; environment only. |
| `FRESHDESK_CALLS_PER_MINUTE` | Positive integer; default 30 per client instance. |
| `FRESHDESK_DESCRIPTION_MAX_LENGTH` | 1-100000 characters; default 2000. |
| `FRESHDESK_ALLOW_SEED` | Unset by default; exactly `1` enables test setup writes. |

## Test Setup and Demo

**Only use a Freshdesk TRIAL or dedicated test account with fictional data.**
`scripts/seed_tickets.py` is the only writing code. It creates 15 fictional
tickets with `fictional-demo` tags and reserved `example.com` requester emails.
Creating a ticket may also create its requester contact. Review notification
and automation settings first: ticket creation can trigger account automations.
The script cannot independently prove that an account is a trial account;
the explicit opt-in is your attestation. Repeated runs create duplicates.

The required requester identity is satisfied with `email`. The script also
supplies name, subject, description, status, priority, source, and tags.
Create-ticket fields, defaults, and requester requirements were checked at
[Create a Ticket](https://developers.freshdesk.com/api/#create_ticket).
Account-specific required custom fields may cause rejection; these are not
guessed or bypassed by the script.

```powershell
$env:FRESHDESK_ALLOW_SEED = "1"
try {
    .venv\Scripts\python scripts\seed_tickets.py
} finally {
    Remove-Item Env:FRESHDESK_ALLOW_SEED -ErrorAction SilentlyContinue
}
.venv\Scripts\python scripts\demo.py
```

Seeding prints a warning, then compact JSON containing created IDs or a sanitized
structured error. It waits before each POST, defaults to 30 calls/minute, honors
lower configured budgets and low remaining-rate headers. Explicit 429 rejection
permits three retries with integer Retry-After or exponential fallback plus
jitter; long server waits are split into sleeps of at most 60 seconds without
retrying early. Stop other account activity to preserve shared API headroom.

The demo launches `python -m freshdesk_mcp.server` with the same interpreter
through the official SDK stdio client. It lists five tickets, fetches at most
one additional list page when `has_more` is true, gets the first ticket with
optional conversations, and searches `tag:'fictional-demo'`. It prints compact
JSON and an `incomplete` record when `has_more` or `truncated` is true; it does
not pretend the bounded sample is a full export. Search indexing can lag, so
newly seeded tickets might not appear immediately. Empty recent listings exit
with `no_demo_ticket`. Run only where displaying fictional ticket text is safe.

### Credential-Free Mock Demo

```powershell
.venv\Scripts\python scripts\demo.py --mock
```

`--mock` always selects mock mode, even if real credentials are present. With
`FRESHDESK_API_KEY` unset or blank, `scripts/demo.py` defaults to mock mode.
The real MCP server still requires credentials; this automatic selection is
limited to the demo script. With a nonblank key and no flag, the unchanged
real-account demo path is selected and normal domain validation applies.

At startup and in its final summary the demo prints:
`MOCK MODE: fictional data, no live Freshdesk account`.
It launches the same MCP server factory over stdio in a child process with an
injected `httpx.MockTransport`. No local port is opened, no HTTP socket is used,
and unknown routes return a fake 404 instead of falling back to a network call.
The child receives only fictional Freshdesk environment values; it does not
receive the caller's account credentials. `*.freshdesk.com` validation is
unchanged: mock requests use a valid fictional hostname, not a localhost
allowlist exception.

The fixture serves ten fictional tickets with varied standard statuses and
priorities, list next-Link headers, embedded conversations and a conversation
route, and search results. The first search attempt returns 429 with integer
`Retry-After: 1`; the existing client retries it. Virtual sleep and clock advance
the retry/limiter without real waits. Mock descriptions are clipped at 80
characters to demonstrate `truncated`. A final `get_ticket(999)` intentionally
returns 200 `text/html`; the demo displays the expected structured
`invalid_response`, then completes successfully. These are deliberately selected
test scenarios, not a general Freshdesk emulator or proof of account behavior.

## Live verification status

**User-reported history:** Live verification against a Freshdesk account was
attempted and was not completed because the API key available at the time was
rejected with **401**. This report is not evidence of a successful live run.
All behavior rests on the referenced documentation and mocked tests. No live
account was contacted while implementing or verifying mock mode.

### Live Demo Output (User Placeholder)

**No completed live demo. No successful live-account results are asserted here.**
Replace this placeholder yourself after running the demo on your trial/test
account. Record date, sanitized output, and observed `has_more`/`truncated`
flags. Never include credentials or real customer data.

## MCP Server

Run `.venv\Scripts\python -m freshdesk_mcp.server` from the project root after
installation, or use the installed `freshdesk-mcp` command. Configure the MCP
host to launch that Python executable with arguments `-m freshdesk_mcp.server`
and supply credentials through its environment. Stdout is reserved for MCP;
startup errors are fixed structured JSON on stderr with a nonzero exit code.
Startup validates configuration and calls `startup_check()` before serving.

Example stdio host configuration (adapt the wrapper to your MCP client's schema):

```json
{
  "mcpServers": {
    "freshdesk": {
      "command": "D:\\Games\\freshdesk-mcp-connector\\.venv\\Scripts\\python.exe",
      "args": ["-m", "freshdesk_mcp.server"],
      "cwd": "D:\\Games\\freshdesk-mcp-connector"
    }
  }
}
```

The host must inherit or securely inject the required environment variables
into its child process. This snippet deliberately contains no credentials and
does not assume environment-substitution syntax supported by every host.
See [Capabilities](docs/CAPABILITIES.md), [Design](docs/DESIGN.md), and
[Tool specification](docs/mcp_tool_spec.json).

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
- Only `scripts/seed_tickets.py`, explicitly marked test setup, writes to
  Freshdesk. The connector remains read-only. Unlike idempotent connector GETs,
  seed POSTs are not replayed after 5xx, transport failures, or malformed success
  responses: creation may have succeeded. Inspect the account before rerunning;
  the script stops without rollback and may have already created some tickets.
  This is a deliberate test-setup exception to the GET retry policy.
- Seed pacing is sequential and conservative, not a distributed account-wide
  limiter. It honors the full seed Retry-After, whereas the unchanged connector
  caps its retry delay at 60 seconds. Neither process controls other API users.
- The demo is a bounded sample, not an export: it fetches at most two list pages
  and one search page. `truncated` cannot recover already-clipped text. Printed
  ticket text remains untrusted data and must never be executed as instructions.
- Mock mode uses an in-process HTTP transport in the stdio child rather than a
  localhost HTTP server. Fictional environment values are supplied only to the
  child, with no changes to the parent's environment or the client's allowlist.
  The mock's virtual time skips wall-clock waits but still exercises the normal
  retry and limiter logic. The HTML case is expected failure data, not a failed
  demo; unexpected errors still produce a failed summary/nonzero exit status.

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
Script tests exercise fictional POST payloads, opt-in, pacing, 429 delays,
ambiguous creation failures, and demo tool calls through an in-memory SDK
session, plus a real stdio child process whose HTTP transport is mocked. These
tests do not create real tickets or prove live-account connectivity.
Mock-demo tests cover explicit/automatic selection, stdio execution without
credentials, pagination, conversations, a retried 429, HTML rejection, and
unchanged real-account domain validation.
Run `python -m pytest -q -p no:cacheprovider` with `.venv\Scripts`
on PATH (or use the explicit interpreter command in Setup).
