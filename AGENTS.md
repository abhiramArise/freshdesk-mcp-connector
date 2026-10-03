# Project Rules

- Read-only Freshdesk MCP connector in Python 3.11+ (official `mcp` SDK, httpx, pytest).
- Fictional/test data only. Credentials via env vars only (FRESHDESK_DOMAIN, FRESHDESK_API_KEY). Never log, print, or put secrets in URLs or error messages. Keep .env.example and .gitignore (excluding .env).
- No create/update/delete in the connector. Only scripts/seed_tickets.py may write, marked as test setup.
- Verify any uncertain Freshdesk behavior against developers.freshdesk.com/api. Do not guess.
- Return structured errors: {error_code, message, retryable, retry_after_seconds}.
- On 429: honor Retry-After plus small jitter, cap retries. Retry 5xx with capped exponential backoff. Never retry other 4xx.
- Run tests after every change and report real results. Do not claim anything works unless you ran it.
- Record ambiguous decisions under "Assumptions" in README.md.
