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
from datetime import date, datetime
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
DEFAULT_TOTAL_TIMEOUT_SECONDS = 45.0
# Search is fixed at 30 results/page, pages 1-10, quoted query <=512 characters.
# https://developers.freshdesk.com/api/#filter_tickets
SEARCH_PER_PAGE = 30
MAX_SEARCH_PAGE = 10
MAX_SEARCH_QUERY_LENGTH = 510
TICKET_FILTERS = ("new_and_my_open", "watching", "spam", "deleted")


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
        try:
            total_timeout = float(os.environ.get("FRESHDESK_TOTAL_TIMEOUT_SECONDS", str(DEFAULT_TOTAL_TIMEOUT_SECONDS)))
        except ValueError:
            total_timeout = 0
        if not math.isfinite(total_timeout) or total_timeout <= 0:
            raise FreshdeskError("configuration_error", "FRESHDESK_TOTAL_TIMEOUT_SECONDS must be a positive finite number.")
        self._total_timeout = total_timeout
        self._timeout_seconds = timeout_seconds
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

    def _deadline_error(self, wait: float = 0) -> FreshdeskError:
        return FreshdeskError("deadline_exceeded", "Freshdesk operation exceeded its total time budget.",
                              retryable=True, retry_after_seconds=wait)

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise self._deadline_error()
        return remaining

    async def _wait(self, wait: float, deadline: float) -> None:
        remaining = deadline - self._clock()
        if wait > remaining:
            raise self._deadline_error(wait)
        self._remaining(deadline)
        try:
            async with asyncio.timeout(remaining):
                await self._sleep(wait)
        except TimeoutError:
            raise self._deadline_error(wait) from None
        self._remaining(deadline)

    async def _acquire_slot(self, deadline: float) -> None:
        # Serialize admission so concurrent requests share the rolling window.
        try:
            async with asyncio.timeout(self._remaining(deadline)):
                await self._limiter_lock.acquire()
        except TimeoutError:
            raise self._deadline_error() from None
        try:
            while True:
                self._remaining(deadline)
                now = self._clock()
                while self._requests and self._requests[0] <= now - MAX_WAIT_SECONDS:
                    self._requests.popleft()
                delay = max(0.0, self._not_before - now)
                if len(self._requests) >= self._calls_per_minute:
                    delay = max(delay, self._requests[0] + MAX_WAIT_SECONDS - now)
                if delay <= 0:
                    self._requests.append(now)
                    return
                await self._wait(delay, deadline)
        finally:
            self._limiter_lock.release()

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
                digits = value.lstrip("0") or "0"
                # Refuse malicious unrepresentable headers rather than retry early.
                if len(digits) > 4096:
                    raise FreshdeskError("invalid_response", "Freshdesk returned an unsupported Retry-After value.")
                seconds = int(digits)
                if seconds > MAX_WAIT_SECONDS:
                    raise FreshdeskError("rate_limited", "Freshdesk requested a wait longer than the retry wait limit.",
                                         retryable=True, retry_after_seconds=seconds)
                base = seconds
        jitter = min(1.0, max(0.0, self._random())) * 0.2
        return min(MAX_WAIT_SECONDS, base + jitter)

    async def _request_tickets(self, params: dict[str, Any], *, path: str = "tickets", deadline: float) -> httpx.Response:
        # Never expose upstream bodies, URLs, or exception text in public failures.
        for attempt in range(MAX_RETRIES + 1):
            await self._acquire_slot(deadline)
            failure = None
            response = None
            try:
                remaining = self._remaining(deadline)
                # httpx timeouts are phase-specific; the outer timer bounds the whole request.
                async with asyncio.timeout(remaining):
                    response = await self._http.get(path, params=params,
                        timeout=httpx.Timeout(min(self._timeout_seconds, remaining)))
            except TimeoutError:
                raise self._deadline_error() from None
            except httpx.TimeoutException:
                failure = ("timeout", "Freshdesk request timed out.")
            except (httpx.NetworkError, httpx.RemoteProtocolError):
                failure = ("connection_error", "Could not connect to Freshdesk.")
            except httpx.RequestError:
                raise FreshdeskError("connection_error", "Freshdesk request could not be completed.") from None
            if response is not None:
                self._remaining(deadline)
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

    async def _read_json(self, path: str, params: dict[str, Any], *, deadline: float | None = None) -> tuple[Any, bool]:
        if deadline is None:
            deadline = self._clock() + self._total_timeout
        response = await self._request_tickets(params, path=path, deadline=deadline)
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
        if data is None:
            raise FreshdeskError("invalid_response", "Freshdesk returned an invalid JSON response.")
        self._remaining(deadline)
        return data, "next" in response.links

    async def _ticket_page(self, params: dict[str, Any], *, deadline: float | None = None) -> tuple[list[dict[str, Any]], bool]:
        data, has_more = await self._read_json("tickets", params, deadline=deadline)
        if not isinstance(data, list) or any(not isinstance(ticket, dict) for ticket in data):
            raise FreshdeskError("invalid_response", "Freshdesk returned an invalid ticket-list response.")
        return data, has_more

    async def list_tickets_page(self, *, page: int = 1, per_page: int = 30,
                                filter: str | None = None, updated_since: str | None = None,
                                include_description: bool = False) -> tuple[list[dict[str, Any]], bool]:
        """Read one page; the existing list_tickets method remains an aggregate."""
        if type(page) is not int or not 1 <= page <= MAX_PAGE or type(per_page) is not int or not 1 <= per_page <= MAX_PER_PAGE:
            raise FreshdeskError("invalid_argument", "Invalid ticket pagination arguments.")
        if filter is not None and filter not in TICKET_FILTERS:
            raise FreshdeskError("invalid_argument", "Unsupported ticket filter.")
        if type(include_description) is not bool:
            raise FreshdeskError("invalid_argument", "include_description must be a boolean.")
        if updated_since is not None:
            valid = False
            if isinstance(updated_since, str):
                try:
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", updated_since):
                        date.fromisoformat(updated_since)
                        valid = True
                    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", updated_since):
                        datetime.fromisoformat(updated_since)
                        valid = True
                except ValueError:
                    pass
            if not valid:
                raise FreshdeskError("invalid_argument", "updated_since must be YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ.")
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        if filter is not None:
            params["filter"] = filter
        if updated_since is not None:
            params["updated_since"] = updated_since
        if include_description:
            params["include"] = "description"
        return await self._ticket_page(params)

    async def get_ticket(self, ticket_id: int, *, include_conversations: bool = False) -> dict[str, Any]:
        if type(ticket_id) is not int or ticket_id <= 0 or type(include_conversations) is not bool:
            raise FreshdeskError("invalid_argument", "ticket_id must be positive and include_conversations must be boolean.")
        params = {"include": "conversations"} if include_conversations else {}
        data, _ = await self._read_json(f"tickets/{ticket_id}", params)
        if not isinstance(data, dict):
            raise FreshdeskError("invalid_response", "Freshdesk returned an invalid ticket response.")
        return data

    async def search_tickets(self, query: str, *, page: int = 1) -> tuple[list[dict[str, Any]], int]:
        if type(page) is not int or not 1 <= page <= MAX_SEARCH_PAGE:
            raise FreshdeskError("invalid_argument", "Search page must be between 1 and 10.")
        valid = isinstance(query, str) and 0 < len(query) <= MAX_SEARCH_QUERY_LENGTH and bool(query.strip())
        if not valid or any(ord(char) < 32 or ord(char) == 127 or char in '\"\\' for char in query):
            raise FreshdeskError("invalid_argument", "Search query must be an unquoted expression of at most 510 characters without controls, double quotes, or backslashes.")
        # Check wrapper safety and balanced grouping; Freshdesk validates field semantics.
        depth = 0
        in_string = False
        for char in query:
            if char == "'":
                in_string = not in_string
            elif not in_string:
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth < 0:
                        valid = False
        if in_string or depth != 0 or not re.search(r"[A-Za-z_][A-Za-z0-9_]*:", query):
            valid = False
        if not valid:
            raise FreshdeskError("invalid_argument", "Search query must contain a field condition and balanced quotes and parentheses.")
        data, _ = await self._read_json("search/tickets", {"query": f'"{query}"', "page": page})
        if (not isinstance(data, dict) or type(data.get("total")) is not int or data["total"] < 0
                or not isinstance(data.get("results"), list)
                or any(not isinstance(ticket, dict) for ticket in data["results"])
                or len(data["results"]) > SEARCH_PER_PAGE):
            raise FreshdeskError("invalid_response", "Freshdesk returned an invalid search response.")
        return data["results"], data["total"]

    async def startup_check(self) -> None:
        """Check authentication and ticket-read permission with one small GET."""
        await self._ticket_page({"page": 1, "per_page": 1})

    async def list_tickets(self, *, page: int = 1, per_page: int = 30, include_description: bool = False) -> tuple[list[dict[str, Any]], bool]:
        """Return (tickets, truncated), stopping at the last link or page cap."""
        if type(page) is not int or not 1 <= page <= MAX_PAGE or type(per_page) is not int or not 1 <= per_page <= MAX_PER_PAGE:
            raise FreshdeskError("invalid_argument", f"page must be 1-{MAX_PAGE} and per_page must be 1-{MAX_PER_PAGE}.")
        tickets = []
        deadline = self._clock() + self._total_timeout
        for current_page in range(page, MAX_PAGE + 1):
            params = {"page": current_page, "per_page": per_page}
            if include_description:
                params["include"] = "description"
            batch, has_next = await self._ticket_page(params, deadline=deadline)
            tickets.extend(batch)
            if not has_next:
                return tickets, False
            if current_page == MAX_PAGE:
                return tickets, True
