"""TEST SETUP ONLY: sole Freshdesk-writing code; never run on production."""
import asyncio
import json
import math
import os
import random
import sys

import httpx
from freshdesk_mcp.client import FreshdeskError, _base_url

SUBJECTS = (
    "Imaginary calendar", "Pretend booking", "Demo password help",
    "Fictional delivery", "Sample invoice", "Test notification",
    "Imaginary subscription", "Pretend access", "Demo export",
    "Fictional address", "Sample search", "Test billing",
    "Imaginary feedback", "Pretend duplicate", "Demo resolution",
)


async def seed_tickets(*, transport=None, sleep=asyncio.sleep, random_source=random.random):
    if os.environ.get("FRESHDESK_ALLOW_SEED") != "1":
        raise FreshdeskError("seed_not_allowed", "Test setup requires FRESHDESK_ALLOW_SEED=1.")
    base_url = _base_url()
    key = os.environ.get("FRESHDESK_API_KEY", "")
    if not key.strip() or key in base_url:
        raise FreshdeskError("configuration_error", "Valid environment credentials are required.")
    try:
        budget = int(os.environ.get("FRESHDESK_CALLS_PER_MINUTE", "30"))
        if budget <= 0:
            raise ValueError
    except ValueError:
        raise FreshdeskError("configuration_error", "FRESHDESK_CALLS_PER_MINUTE must be a positive integer.") from None
    interval = 60 / min(budget, 30)
    spacing = interval
    ids = []

    async def wait(seconds):
        # Honor the full server delay with bounded individual sleeps.
        while seconds > 0:
            chunk = min(seconds, 60)
            await sleep(chunk)
            seconds -= chunk

    async with httpx.AsyncClient(base_url=base_url, auth=httpx.BasicAuth(key, "X"),
            timeout=httpx.Timeout(10), transport=transport,
            follow_redirects=False, trust_env=False) as client:
        for index, subject in enumerate(SUBJECTS):
            payload = {"email": f"fictional-demo-{index + 1:02d}@example.com",
                "name": f"Fictional Requester {index + 1:02d}",
                "subject": f"FICTIONAL TEST: {subject}",
                "description": "Fictional test setup only. No real customer or incident is represented.",
                "status": 2 + index % 4, "priority": 1 + index % 4,
                "source": 2, "tags": ["fictional-demo"]}
            for attempt in range(4):
                await wait(spacing)
                try:
                    response = await client.post("tickets", json=payload)
                except httpx.RequestError:
                    raise FreshdeskError("ambiguous_create", "Creation outcome is unknown; inspect the test account before rerunning.") from None
                try:
                    remaining = float(response.headers["X-RateLimit-Remaining"])
                    total = float(response.headers["X-RateLimit-Total"])
                    if math.isfinite(remaining) and math.isfinite(total) and remaining >= 0 and total > 0:
                        spacing = max(interval, 60 / max(1, remaining)) if remaining <= max(1, total * 0.1) else interval
                except (KeyError, ValueError):
                    pass
                if response.status_code == 429:
                    try:
                        delay = int(response.headers.get("Retry-After", ""))
                        if delay < 0:
                            raise ValueError
                    except ValueError:
                        delay = 2 ** attempt
                    delay += max(0, min(1, random_source())) * 0.2
                    if attempt == 3:
                        raise FreshdeskError("rate_limited", "Test setup rate limit exhausted.", True, delay)
                    await wait(delay)
                    continue
                if response.status_code >= 500:
                    raise FreshdeskError("ambiguous_create", "Creation outcome is unknown; inspect the test account before rerunning.")
                if response.status_code != 201:
                    code = {401: "authentication_failed", 403: "access_denied"}.get(response.status_code, "http_error")
                    raise FreshdeskError(code, "Freshdesk rejected test ticket creation.")
                try:
                    ticket_id = response.json()["id"]
                    if type(ticket_id) is not int or ticket_id <= 0:
                        raise ValueError
                except (ValueError, KeyError, TypeError):
                    raise FreshdeskError("ambiguous_create", "Creation response was invalid; inspect the test account before rerunning.") from None
                ids.append(ticket_id)
                break
    return ids


def main():
    print("WARNING: Test setup writes 15 tickets. Use only a trial or test account; reruns create duplicates.", file=sys.stderr)
    try:
        ids = asyncio.run(seed_tickets())
        result, code = {"created_count": len(ids), "ticket_ids": ids}, 0
    except FreshdeskError as error:
        result, code = error.to_dict(), 1
    except Exception:
        result, code = FreshdeskError("internal_error", "Test setup failed; inspect the test account before rerunning.").to_dict(), 1
    output = json.dumps(result, separators=(",", ":"))
    key = os.environ.get("FRESHDESK_API_KEY", "")
    if key:
        output = output.replace(json.dumps(key)[1:-1], "[REDACTED]").replace(key, "[REDACTED]")
    print(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
