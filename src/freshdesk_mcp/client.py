"""Read-only Freshdesk HTTP client with bounded retries and rate limiting."""

import asyncio
import math
import os
import random
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

# Maximum objects per page: https://developers.freshdesk.com/api/#pagination
MAX_PER_PAGE = 100
# Ticket listing allows 300 pages: https://developers.freshdesk.com/api/#pagination
# Ticket-specific limit: https://developers.freshdesk.com/api/#list_all_tickets
MAX_PAGE = 300
MAX_RETRIES = 3
MAX_WAIT_SECONDS = 60.0
DEFAULT_CALLS_PER_MINUTE = 30


@dataclass(frozen=True)
class _Error:
    error_code: str
    message: str
    retryable: bool = False
    retry_after_seconds: float | None = None


class FreshdeskError(Exception):
    """Sanitized failure with a stable, serializable public representation."""

    def __init__(self, error_code: str, message: str, retryable: bool = False, retry_after_seconds: float | None = None):
        self._error = _Error(error_code, message, retryable, retry_after_seconds)
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self._error)


def _base_url() -> str:
    domain = os.environ.get("FRESHDESK_DOMAIN", "").strip()
    value = domain if "://" in domain else f"https://{domain}"
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.hostname.endswith(".freshdesk.com")
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and parsed.path in ("", "/")
            and parsed.port in (None, 443)
            and re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname)
        )
    except ValueError:
        valid = False
    if not valid:
        raise FreshdeskError("configuration_error", "FRESHDESK_DOMAIN must identify an HTTPS host ending in .freshdesk.com without credentials, path, or query.")
    return f"https://{parsed.hostname}/api/v2/"


class FreshdeskClient:
    """Load credentials from environment; use as an async context manager."""

    def __init__(self, *, timeout_seconds: float = 10.0, transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 random_source: Callable[[], float] = random.random,
                 clock: Callable[[], float] = time.monotonic):
        base_url = _base_url()
        key = os.environ.get("FRESHDESK_API_KEY", "")
        if not key.strip():
            raise FreshdeskError("configuration_error", "FRESHDESK_API_KEY is required.")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise FreshdeskError("configuration_error", "Request timeout must be a positive finite number.")
        calls_per_minute = None
        try:
            calls_per_minute = int(os.environ.get("FRESHDESK_CALLS_PER_MINUTE", str(DEFAULT_CALLS_PER_MINUTE)))
        except ValueError:
            pass
        if calls_per_minute is None or calls_per_minute <= 0:
            raise FreshdeskError("configuration_error", "FRESHDESK_CALLS_PER_MINUTE must be a positive integer.")
        self._calls_per_minute = calls_per_minute
        self._sleep = sleep
        self._random = random_source
        self._clock = clock
        self._requests: deque[float] = deque()
        self._limiter_lock = asyncio.Lock()
        self._not_before = 0.0
        self._http = httpx.AsyncClient(
            base_url=base_url,
            auth=httpx.BasicAuth(key, "X"),
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json"},
        )

    async def __aenter__(self) -> "FreshdeskClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _acquire_slot(self) -> None:
        # Serialize admission so concurrent requests share the rolling window.
        async with self._limiter_lock:
            while True:
                now = self._clock()
                while self._requests and self._requests[0] <= now - MAX_WAIT_SECONDS:
                    self._requests.popleft()
                delay = max(0.0, self._not_before - now)
                if len(self._requests) >= self._calls_per_minute:
                    delay = max(delay, self._requests[0] + MAX_WAIT_SECONDS - now)
                if delay <= 0:
                    self._requests.append(now)
                    return
                await self._sleep(min(MAX_WAIT_SECONDS, delay))

    def _read_rate_limit(self, response: httpx.Response) -> None:
        try:
            remaining = float(response.headers["X-RateLimit-Remaining"])
            total = float(response.headers["X-RateLimit-Total"])
        except (KeyError, ValueError):
            return
        if not math.isfinite(remaining) or not math.isfinite(total) or total <= 0 or not 0 <= remaining <= total:
            return
        if remaining <= max(1.0, total * 0.1):
            delay = min(MAX_WAIT_SECONDS, MAX_WAIT_SECONDS / max(1.0, remaining))
            self._not_before = max(self._not_before, self._clock() + delay)

    def _retry_delay(self, attempt: int, response: httpx.Response | None) -> float:
        base = float(2 ** attempt)
        if response is not None and response.status_code == 429:
            value = response.headers.get("Retry-After", "").strip()
            if re.fullmatch(r"[0-9]+", value):
                # Bound the string before conversion, including extremely long headers.
                digits = value.lstrip("0") or "0"
                base = MAX_WAIT_SECONDS if len(digits) > 2 else min(MAX_WAIT_SECONDS, int(digits))
        jitter = min(1.0, max(0.0, self._random())) * 0.2
        return min(MAX_WAIT_SECONDS, base + jitter)

    async def _request_tickets(self, params: dict[str, Any]) -> httpx.Response:
        # Never expose upstream bodies, URLs, or exception text in public failures.
        for attempt in range(MAX_RETRIES + 1):
            await self._acquire_slot()
            failure = None
            response = None
            try:
                response = await self._http.get("tickets", params=params)
            except httpx.TimeoutException:
                failure = ("timeout", "Freshdesk request timed out.")
            except httpx.RequestError:
                failure = ("connection_error", "Could not connect to Freshdesk.")
            if response is not None:
                self._read_rate_limit(response)
                if response.status_code == 429:
                    failure = ("rate_limited", "Freshdesk rate limit reached (429).")
                elif 500 <= response.status_code <= 599:
                    failure = ("http_error", "Freshdesk returned an unsuccessful HTTP response.")
                else:
                    return response
            delay = self._retry_delay(attempt, response)
            self._not_before = max(self._not_before, self._clock() + delay)
            if attempt == MAX_RETRIES:
                assert failure is not None
                raise FreshdeskError(*failure, retryable=True, retry_after_seconds=delay)

    async def _ticket_page(self, params: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        response = await self._request_tickets(params)
        if not 200 <= response.status_code < 300:
            codes = {
                401: ("authentication_failed", "Freshdesk authentication failed (401). Check FRESHDESK_API_KEY."),
                403: ("access_denied", "Freshdesk access denied (403). Check agent permissions."),
                404: ("not_found", "Freshdesk endpoint was not found (404). Check FRESHDESK_DOMAIN."),
                429: ("rate_limited", "Freshdesk rate limit reached (429)."),
            }
            code, message = codes.get(response.status_code, ("http_error", "Freshdesk returned an unsuccessful HTTP response."))
            raise FreshdeskError(code, message, response.status_code == 429 or response.status_code >= 500)
        data = None
        try:
            data = response.json()
        except ValueError:
            pass
        if not isinstance(data, list) or any(not isinstance(ticket, dict) for ticket in data):
            raise FreshdeskError("invalid_response", "Freshdesk returned an invalid ticket-list response.")
        return data, "next" in response.links

    async def startup_check(self) -> None:
        """Check authentication and ticket-read permission with one small GET."""
        await self._ticket_page({"page": 1, "per_page": 1})

    async def list_tickets(self, *, page: int = 1, per_page: int = 30, include_description: bool = False) -> tuple[list[dict[str, Any]], bool]:
        """Return (tickets, truncated), stopping at the last link or page cap."""
        if type(page) is not int or not 1 <= page <= MAX_PAGE or type(per_page) is not int or not 1 <= per_page <= MAX_PER_PAGE:
            raise FreshdeskError("invalid_argument", f"page must be 1-{MAX_PAGE} and per_page must be 1-{MAX_PER_PAGE}.")
        tickets = []
        for current_page in range(page, MAX_PAGE + 1):
            params = {"page": current_page, "per_page": per_page}
            if include_description:
                params["include"] = "description"
            batch, has_next = await self._ticket_page(params)
            tickets.extend(batch)
            if not has_next:
                return tickets, False
            if current_page == MAX_PAGE:
                return tickets, True
