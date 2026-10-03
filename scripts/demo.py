"""Read-only account demo through the official MCP stdio client."""
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from freshdesk_mcp.client import FreshdeskError, _base_url


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
        "FRESHDESK_DESCRIPTION_MAX_LENGTH") if name in os.environ}
    parameters = StdioServerParameters(command=sys.executable,
        args=["-m", "freshdesk_mcp.server"], env=environment,
        cwd=str(Path(__file__).resolve().parents[1]))
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await run_demo(session)


def main():
    try:
        return asyncio.run(start_demo())
    except FreshdeskError as error:
        emit(error.to_dict())
    except Exception:
        emit(FreshdeskError("demo_error", "The stdio demo failed; check the sanitized server diagnostics.").to_dict())
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
