# Design

## Current Test Connector

API-key Basic authentication is documented by Freshdesk: the key is the
username and `X` is a dummy password
([authentication](https://developers.freshdesk.com/api/#authentication)).
`client.py` reads credentials from environment variables, validates an HTTPS
Freshdesk origin, disables redirects/proxy inheritance, and uses HTTP timeouts.
This simple single-account test setup is not a credential lifecycle solution.

The existing client retries only 429, 5xx, and transport failures, at most three
times. Integer Retry-After or exponential backoff receives small jitter and a
60-second wait cap. Every attempt enters a sliding-window limiter; low-budget
headers add cooldown. Missing/invalid headers are ignored. These are local
policies; [Freshdesk rate limits](https://developers.freshdesk.com/api/#rate-limit)
are account-wide and include other applications. There is no overall per-call
time budget. The seed script separately paces POSTs and does not replay uncertain
creation outcomes; see README Assumptions. Connector behavior is unchanged.

`server.py` marks all ticket/conversation text `customer_provided`, clips long
bodies, redacts echoed credentials, and warns in every tool description that
text is untrusted data, never instructions. It returns structured sanitized
errors instead of exposing upstream exception details. Consumers must maintain
that boundary; tool annotations alone do not make customer text trustworthy.

## Production Work Still Needed

These are proposed requirements, not implemented or tested capabilities:

- Replace static single-account credentials with an appropriate OAuth flow or
  a Freshworks Marketplace app deployment. Verify support and scopes for the
  exact product first; do not assume this ticket API already accepts OAuth.
  Freshworks documents [app OAuth setup](https://developers.freshworks.com/docs/tutorials/intermediate/request-method/setup-oauth/).
- Store and rotate secrets with a secrets manager; isolate tenant credentials.
- Coordinate limits per tenant/account across processes and budget embedded
  API credits; add overall deadlines and operational monitoring.
- Add audit logging of permitted operation metadata and outcomes, never keys,
  Authorization headers, raw bodies, or customer text by default.
- Prefer authenticated, replay-protected event delivery over repeated polling.
  Evaluate [Freshdesk webhooks](https://support.freshdesk.com/support/articles/132589-using-webhooks-in-the)
  or [Freshworks ticket product events](https://developers.freshworks.com/docs/app-sdk/v3.0/support_ticket/serverless-apps/product-events/)
  with idempotent consumers and documented account/plan requirements.

No live-account verification has been performed for this stage.
