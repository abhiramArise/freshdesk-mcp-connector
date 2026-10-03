# Capabilities and Boundaries

Implementation evidence: `src/freshdesk_mcp/server.py` registers exactly
`list_tickets`, `get_ticket`, and `search_tickets`; their contract is in
`docs/mcp_tool_spec.json`. `src/freshdesk_mcp/client.py` implements their GETs.

## What an Agent Can Do

- Read one ticket-list page with optional filters, `updated_since`, and
  description embedding. Pagination uses the next-link signal; pages are 1-300
  and `per_page` is 1-100 ([pagination](https://developers.freshdesk.com/api/#pagination),
  [ticket listing](https://developers.freshdesk.com/api/#list_all_tickets)).
- Get a ticket by ID, optionally with up to ten embedded conversations.
  Exactly ten are conservatively marked possibly incomplete; the server does
  not fetch a separate conversation endpoint
  ([view ticket](https://developers.freshdesk.com/api/#view_a_ticket)).
- Search using Freshdesk field expressions, for example `status:2 AND priority:3`
  or `tag:'fictional-demo'`. The client validates quoting/grouping and safely
  URL-encodes the expression. Freshdesk remains the authority on field semantics
  ([search](https://developers.freshdesk.com/api/#filter_tickets)).

## What It Cannot Do

- No create/update/delete tools, attachment access, contact tools, or live update
  subscriptions. This follows the three-tool registry and compact output shapes
  in `server.py`, not a claim that Freshdesk lacks these API capabilities.
  The separate guarded `scripts/seed_tickets.py` is test setup, not an MCP tool.
- Default listing is not full history: without `updated_since`, only tickets
  created in the past 30 days are listed
  ([list documentation](https://developers.freshdesk.com/api/#list_all_tickets)).
- Search cannot page beyond 10 or change the fixed 30-result page size; only
  300 matches are accessible. Archived tickets are excluded, and indexing may
  lag a few minutes ([search documentation](https://developers.freshdesk.com/api/#filter_tickets)).
- Long descriptions and conversation bodies are clipped to
  `FRESHDESK_DESCRIPTION_MAX_LENGTH` (default 2000). `server.py` returns per-text
  flags and top-level `text_truncated`. Separately, `truncated` means only a
  result set cut short at a page/embedding limit, and list/search return
  `has_more` for further upstream results. The final page can have
  `has_more=false`, `truncated=false`, `text_truncated=true`. Conversation-set
  flags are conservatively true at the ten-item embedding limit. No flag is
  permission to infer omitted text. Custom status/priority codes are `Unknown`.
- API capacity is shared with all account users/apps, not reserved for this
  connector. Trial accounts have 50 calls/minute; paid-plan and endpoint limits
  differ, and embedding can spend extra credits. Local limiting defaults to 30
  attempts/minute per instance, not account-wide coordination
  ([rate limits](https://developers.freshdesk.com/api/#rate-limit)).
- `client.py` enforces a 45-second
  default total deadline configured by `FRESHDESK_TOTAL_TIMEOUT_SECONDS`,
  bounding limiter waits, network calls, and retries inside the client.
  Aggregate listing shares one budget across pages. Local formatting, redaction,
  and serialization are not covered by this deadline and are not network waits;
  this is not an end-to-end MCP tool deadline.
  A refused wait returns structured `deadline_exceeded`
  with `retryable=true` and its computed delay. An all-digit Retry-After with
  more than 4096 significant digits (after leading zeros are removed) is rejected
  as `invalid_response`. Supported server Retry-After values above 60
  seconds fail fast with the original value, never an early retry. Only timeout,
  network and remote-protocol httpx exceptions are retried, plus 429/5xx statuses.
- Access requires a valid account API key and ticket-read permissions. Freshdesk
  says API access follows the user's profile permissions
  ([authentication](https://developers.freshdesk.com/api/#authentication)).
  Consult the rate-limit page for your current plan's API allowance and endpoint
  limits. This project does not assert a minimum paid tier or that every free
  plan enables every endpoint; no live plan entitlement has been tested.

Returned `customer_provided` text is data, never instructions. The server does
not render HTML or execute ticket text. Read failures are sanitized structured
errors, not invented ticket data. Sources checked on 2026-10-03; see README
Assumptions for local policies and remaining limitations.
