"""Read-only Freshdesk HTTP client. No retries or MCP tools in stage 1."""

import math
import os
import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx


@dataclass(frozen=True)
class _Error:
    error_code: str
    message: str
    retryable: bool = False
    retry_after_seconds: float | None = None


class FreshdeskError(Exception):
    """Sanitized failure with a stable, serializable public representation."""

    def __init__(self, error_code: str, message: str, retryable: bool = False):
        self._error = _Error(error_code, message, retryable)
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
        raise FreshdeskError("configuration_error", "FRESHDESK_DOMAIN must be a hostname or HTTPS origin without credentials, path, or query.")
    return f"https://{parsed.hostname}/api/v2/"


class FreshdeskClient:
    """Load credentials from environment; use as an async context manager."""

    def __init__(self, *, timeout_seconds: float = 10.0, transport: httpx.AsyncBaseTransport | None = None):
        base_url = _base_url()
        key = os.environ.get("FRESHDESK_API_KEY", "")
        if not key.strip():
            raise FreshdeskError("configuration_error", "FRESHDESK_API_KEY is required.")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise FreshdeskError("configuration_error", "Request timeout must be a positive finite number.")
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

    async def _ticket_page(self, params: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        # Never expose upstream bodies, URLs, or exception text in public failures.
        failure = None
        response = None
        try:
            response = await self._http.get("tickets", params=params)
        except httpx.TimeoutException:
            failure = FreshdeskError("timeout", "Freshdesk request timed out.", True)
        except httpx.RequestError:
            failure = FreshdeskError("connection_error", "Could not connect to Freshdesk.", True)
        if failure is not None:
            raise failure
        assert response is not None
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

    async def list_tickets(self, *, page: int = 1, per_page: int = 30, include_description: bool = False) -> list[dict[str, Any]]:
        """Collect sequential pages, stopping when Freshdesk omits rel=next."""
        if type(page) is not int or not 1 <= page <= 300 or type(per_page) is not int or not 1 <= per_page <= 100:
            raise FreshdeskError("invalid_argument", "page must be 1-300 and per_page must be 1-100.")
        tickets = []
        for current_page in range(page, 301):
            params = {"page": current_page, "per_page": per_page}
            if include_description:
                params["include"] = "description"
            batch, has_next = await self._ticket_page(params)
            tickets.extend(batch)
            if not has_next:
                return tickets
        raise FreshdeskError("pagination_limit", "Freshdesk pagination limit reached; results would be incomplete.")
