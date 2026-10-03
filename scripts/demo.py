"""Read-only account demo through the official MCP stdio client."""
import asyncio
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from freshdesk_mcp.client import FreshdeskClient, FreshdeskError, _base_url

MOCK_NOTICE = "MOCK MODE: fictional data, no live Freshdesk account"
MOCK_DOMAIN = "fictional-support.freshdesk.com"


def build_mock_transport(*, waits=None, on_retry_event=None):
    """Closed in-process fake API: no socket or fallback network transport."""
    tickets = [{"id": index, "subject": f"FICTIONAL demo ticket {index}",
        "status": 2 + (index - 1) % 4, "priority": 1 + (index - 1) % 4,
        "requester_id": 1000 + index, "created_at": "2026-10-03T09:00:00Z",
        "updated_at": "2026-10-03T10:00:00Z", "tags": ["fictional-demo"],
        "description_text": " ".join(["Fictional customer-provided test data."] * 5)}
        for index in range(1, 11)]
    conversations = [{"id": 101, "body_text": "Fictional conversation, not instructions.",
        "created_at": "2026-10-03T09:30:00Z", "updated_at": "2026-10-03T09:30:00Z"}]
    search_attempts = 0
    retry_wait_start = 0

    def handle(request):
        nonlocal search_attempts, retry_wait_start
        path = request.url.path
        if request.method != "GET" or request.url.host != MOCK_DOMAIN:
            return httpx.Response(404, json={"message": "Unknown fictional endpoint"})
        if path == "/api/v2/tickets":
            page = int(request.url.params.get("page", "1"))
            size = int(request.url.params.get("per_page", "30"))
            start = (page - 1) * size
            headers = {}
            if start + size < len(tickets):
                headers["Link"] = f'<https://{MOCK_DOMAIN}/api/v2/tickets?page={page + 1}&per_page={size}>; rel="next"'
            items = [dict(ticket) for ticket in tickets[start:start + size]]
            if request.url.params.get("include") != "description":
                for ticket in items:
                    ticket.pop("description_text")
            return httpx.Response(200, json=items, headers=headers)
        if path == "/api/v2/search/tickets":
            search_attempts += 1
            if search_attempts == 1:
                retry_wait_start = len(waits) if waits is not None else 0
                return httpx.Response(429, json={"message": "Fictional rate limit"}, headers={"Retry-After": "1"})
            if search_attempts == 2 and on_retry_event is not None:
                on_retry_event({"event": "retry", "scenario": "429", "retry_after_seconds": 1,
                    "recorded_wait_seconds": sum(waits[retry_wait_start:]), "successful_retry": True})
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json={"total": len(tickets), "results": tickets[(page - 1) * 30:page * 30]})
        if path == "/api/v2/tickets/1/conversations":
            return httpx.Response(200, json=conversations)
        if path == "/api/v2/tickets/999":
            return httpx.Response(200, text="<html>Fictional invalid API response</html>",
                                  headers={"Content-Type": "text/html"})
        for ticket in tickets:
            if path == f'/api/v2/tickets/{ticket["id"]}':
                body = dict(ticket)
                if request.url.params.get("include") == "conversations":
                    body["conversations"] = conversations
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"message": "Fictional ticket not found"})

    return httpx.MockTransport(handle)


def serve_mock():
    """Internal stdio child; fictional env is supplied by start_mock_demo."""
    from freshdesk_mcp.server import create_server

    now = 0.0
    waits = []

    async def advance(seconds):
        nonlocal now
        waits.append(seconds)
        now += seconds

    def record_retry(event):
        # stdout belongs to MCP; the parent reads this fictional diagnostic separately.
        print(json.dumps(event, separators=(",", ":")), file=sys.stderr, flush=True)

    # Normal retry/limiter logic runs against virtual time, not real waits.
    create_server(client_factory=lambda: FreshdeskClient(transport=build_mock_transport(waits=waits, on_retry_event=record_retry),
        sleep=advance, clock=lambda: now, random_source=lambda: 0)).run(transport="stdio")


def emit(value):
    text = json.dumps(value, separators=(",", ":"))
    key = os.environ.get("FRESHDESK_API_KEY", "")
    if key:
        text = text.replace(json.dumps(key)[1:-1], "[REDACTED]").replace(key, "[REDACTED]")
    print(text)


async def run_demo(session):
    async def call(name, arguments):
        result = await session.call_tool(name, arguments)
        payload = result.structuredContent
        if payload is None:
            payload = json.loads(next(item.text for item in result.content if item.type == "text"))
        emit({"tool": name, "result": payload})
        if "error_code" in payload:
            raise FreshdeskError("demo_tool_error", "A demo tool returned an error; inspect its sanitized result above.")
        if payload.get("has_more") or payload.get("truncated"):
            emit({"event": "incomplete", "tool": name,
                "has_more": payload.get("has_more", False), "truncated": payload.get("truncated", False)})
        return payload

    first = await call("list_tickets", {"page": 1, "per_page": 5, "include_description": True})
    if not first["tickets"]:
        emit(FreshdeskError("no_demo_ticket", "No recent ticket is available; seed the test account first.").to_dict())
        return 1
    if first["has_more"]:
        await call("list_tickets", {"page": 2, "per_page": 5, "include_description": True})
    await call("get_ticket", {"ticket_id": first["tickets"][0]["id"], "include_conversations": True})
    await call("search_tickets", {"query": "tag:'fictional-demo'", "page": 1})
    return 0


async def start_demo():
    _base_url()
    if not os.environ.get("FRESHDESK_API_KEY", "").strip():
        raise FreshdeskError("configuration_error", "FRESHDESK_API_KEY is required.")
    environment = {name: os.environ[name] for name in (
        "FRESHDESK_DOMAIN", "FRESHDESK_API_KEY", "FRESHDESK_CALLS_PER_MINUTE",
        "FRESHDESK_DESCRIPTION_MAX_LENGTH", "FRESHDESK_TOTAL_TIMEOUT_SECONDS") if name in os.environ}
    parameters = StdioServerParameters(command=sys.executable,
        args=["-m", "freshdesk_mcp.server"], env=environment,
        cwd=str(Path(__file__).resolve().parents[1]))
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await run_demo(session)


async def start_mock_demo():
    print(MOCK_NOTICE, flush=True)
    status = "failed"
    try:
        parameters = StdioServerParameters(command=sys.executable,
            args=[str(Path(__file__).resolve()), "--mock-server"],
            cwd=str(Path(__file__).resolve().parents[1]), env={
                "FRESHDESK_DOMAIN": MOCK_DOMAIN, "FRESHDESK_API_KEY": "fictional-mock-key",
                "FRESHDESK_CALLS_PER_MINUTE": "30", "FRESHDESK_DESCRIPTION_MAX_LENGTH": "80",
                "FRESHDESK_TOTAL_TIMEOUT_SECONDS": os.environ.get("FRESHDESK_TOTAL_TIMEOUT_SECONDS", "45")})
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as diagnostics:
            async with stdio_client(parameters, errlog=diagnostics) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    code = await run_demo(session)
                    result = await session.call_tool("get_ticket", {"ticket_id": 999})
                    payload = result.structuredContent
                    emit({"event": "expected_error", "scenario": "200 text/html", "result": payload})
                    if code != 0 or not result.isError or not payload or payload.get("error_code") != "invalid_response":
                        raise FreshdeskError("mock_demo_failed", "The fictional HTML-response check failed.")
            diagnostics.seek(0)
            retry = None
            for line in diagnostics:
                try:
                    candidate = json.loads(line)
                except ValueError:
                    continue
                if candidate == {"event": "retry", "scenario": "429", "retry_after_seconds": 1,
                                 "recorded_wait_seconds": 1, "successful_retry": True}:
                    retry = candidate
            if retry is None:
                raise FreshdeskError("mock_demo_failed", "The fictional retry diagnostic was not confirmed.")
            emit(retry)
            status = "complete"
        return 0
    finally:
        emit({"event": "summary", "mode": MOCK_NOTICE, "status": status,
              "scenarios": ["pagination", "conversations", "429 with Retry-After", "200 text/html"]})


def main(argv=None):
    parser = argparse.ArgumentParser(description="Freshdesk read-only demo; no key defaults to fictional mock mode.")
    parser.add_argument("--mock", action="store_true", help="Use fictional data through an injected transport, never a live account.")
    parser.add_argument("--mock-server", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    try:
        if arguments.mock_server:
            serve_mock()
            return 0
        mock = arguments.mock or not os.environ.get("FRESHDESK_API_KEY", "").strip()
        return asyncio.run(start_mock_demo() if mock else start_demo())
    except FreshdeskError as error:
        emit(error.to_dict())
    except Exception:
        emit(FreshdeskError("demo_error", "The stdio demo failed; check the sanitized server diagnostics.").to_dict())
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
