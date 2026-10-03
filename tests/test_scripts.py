import base64
import importlib.metadata
import json
import sys
import tomllib
from pathlib import Path

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from freshdesk_mcp import FreshdeskClient, FreshdeskError
from freshdesk_mcp.server import create_server
from scripts import demo, seed_tickets


KEY = "fictional-test-key"


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("FRESHDESK_DOMAIN", "fictional-support.freshdesk.com")
    monkeypatch.setenv("FRESHDESK_API_KEY", KEY)
    monkeypatch.delenv("FRESHDESK_ALLOW_SEED", raising=False)
    monkeypatch.delenv("FRESHDESK_TOTAL_TIMEOUT_SECONDS", raising=False)


async def test_seed_refuses_without_opt_in(runtime):
    calls = []
    with pytest.raises(FreshdeskError) as exc:
        await seed_tickets.seed_tickets(transport=httpx.MockTransport(lambda request: calls.append(request)), sleep=runtime.sleep)
    assert exc.value.to_dict()["error_code"] == "seed_not_allowed"
    assert calls == []
    assert runtime.waits == []


async def test_seed_fictional_payloads_and_pacing(monkeypatch, runtime, caplog):
    monkeypatch.setenv("FRESHDESK_ALLOW_SEED", "1")
    requests = []
    times = []

    def handler(request):
        requests.append(request)
        times.append(runtime.now)
        assert request.method == "POST"
        assert request.url.path == "/api/v2/tickets"
        assert request.headers["authorization"] == "Basic " + base64.b64encode(f"{KEY}:X".encode()).decode()
        assert KEY not in str(request.url)
        return httpx.Response(201, json={"id": len(requests), "description": KEY})

    result = await seed_tickets.seed_tickets(transport=httpx.MockTransport(handler), sleep=runtime.sleep)
    assert result == list(range(1, 16))
    payloads = [json.loads(request.content) for request in requests]
    assert len(payloads) == 15
    assert all(payload["email"].split("@")[1] == "example.com" for payload in payloads)
    assert all("FICTIONAL" in payload["subject"] and "fictional-demo" in payload["tags"] for payload in payloads)
    assert len({payload["subject"] for payload in payloads}) == 15
    assert {payload["status"] for payload in payloads} == {2, 3, 4, 5}
    assert {payload["priority"] for payload in payloads} == {1, 2, 3, 4}
    assert all({"email", "name", "subject", "description", "status", "priority", "source", "tags"} <= set(payload) for payload in payloads)
    assert times == list(range(2, 31, 2))
    assert KEY not in caplog.text


@pytest.mark.parametrize("value", ["", "0", "yes", "true", KEY])
async def test_seed_requires_exact_opt_in(monkeypatch, value, runtime):
    monkeypatch.setenv("FRESHDESK_ALLOW_SEED", value)
    with pytest.raises(FreshdeskError) as exc:
        await seed_tickets.seed_tickets(sleep=runtime.sleep)
    assert KEY not in str(exc.value)
    assert KEY not in repr(exc.value)


@pytest.mark.parametrize("header,expected", [("10", 10), ("120", 120), ("invalid", 1), ("-1", 1), (None, 1)])
async def test_seed_429_then_success(monkeypatch, runtime, header, expected):
    monkeypatch.setenv("FRESHDESK_ALLOW_SEED", "1")
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, text=KEY, headers={} if header is None else {"Retry-After": header})
        return httpx.Response(201, json={"id": len(calls)})

    result = await seed_tickets.seed_tickets(transport=httpx.MockTransport(handler), sleep=runtime.sleep, random_source=lambda: 0)
    assert len(result) == 15
    assert len(calls) == 16
    assert sum(runtime.waits) == 32 + expected
    assert max(runtime.waits) <= 60


@pytest.mark.parametrize("failure,code", [(429, "rate_limited"), (401, "authentication_failed"), (403, "access_denied"), (400, "http_error"), (500, "ambiguous_create"), ("timeout", "ambiguous_create"), ("connection", "ambiguous_create"), ("malformed", "ambiguous_create")])
async def test_seed_sanitized_errors_no_ambiguous_replays(monkeypatch, runtime, failure, code, caplog):
    monkeypatch.setenv("FRESHDESK_ALLOW_SEED", "1")
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout(KEY)
        if failure == "connection":
            raise httpx.ConnectError(KEY)
        return httpx.Response(201 if failure == "malformed" else failure, text=KEY, headers={"Retry-After": "1"})

    with pytest.raises(FreshdeskError) as exc:
        await seed_tickets.seed_tickets(transport=httpx.MockTransport(handler), sleep=runtime.sleep, random_source=lambda: 0)
    assert exc.value.to_dict()["error_code"] == code
    assert len(calls) == (4 if failure == 429 else 1)
    assert KEY not in str(exc.value)
    assert KEY not in repr(exc.value)
    assert KEY not in caplog.text


async def test_seed_low_budget_and_configured_pacing(monkeypatch, runtime):
    monkeypatch.setenv("FRESHDESK_ALLOW_SEED", "1")
    monkeypatch.setenv("FRESHDESK_CALLS_PER_MINUTE", "10")
    calls = []

    def handler(request):
        calls.append(runtime.now)
        return httpx.Response(201, json={"id": len(calls)}, headers={"X-RateLimit-Remaining": "1.0", "X-RateLimit-Total": "50.0"})

    await seed_tickets.seed_tickets(transport=httpx.MockTransport(handler), sleep=runtime.sleep)
    assert calls[0] == 6
    assert calls[1] == 66


def test_seed_main_warning_and_no_key(monkeypatch, capsys):
    assert seed_tickets.main() == 1
    output = capsys.readouterr()
    assert "trial or test account" in output.err
    assert KEY not in output.out + output.err
    assert json.loads(output.out)["error_code"] == "seed_not_allowed"


async def test_demo_calls_all_tools_and_handles_flags(runtime, capsys):
    calls = []

    def handler(request):
        calls.append(request)
        ticket = {"id": 7, "subject": "FICTIONAL demo", "status": 2, "priority": 1, "description_text": KEY + " description"}
        if len(calls) == 1:
            return httpx.Response(200, json=[])
        if request.url.path == "/api/v2/search/tickets":
            assert request.url.params["query"] == '"tag:\'fictional-demo\'"'
            return httpx.Response(200, json={"total": 1, "results": [ticket]})
        if request.url.path == "/api/v2/tickets/7":
            return httpx.Response(200, json=ticket)
        headers = {"Link": '<https://fictional-support.freshdesk.com/api/v2/tickets?page=2>; rel="next"'} if request.url.params["page"] == "1" else {}
        return httpx.Response(200, json=[ticket], headers=headers)

    server = create_server(client_factory=lambda: FreshdeskClient(transport=httpx.MockTransport(handler)))
    async with create_connected_server_and_client_session(server) as session:
        assert await demo.run_demo(session) == 0
    output = capsys.readouterr()
    records = [json.loads(line) for line in output.out.splitlines()]
    assert {record.get("tool") for record in records} >= {"list_tickets", "get_ticket", "search_tickets"}
    assert any(record.get("event") == "incomplete" and record["has_more"] for record in records)
    assert KEY not in output.out + output.err
    assert all(request.method == "GET" for request in calls)


async def test_demo_empty_account_reports_no_ticket(capsys):
    server = create_server(client_factory=lambda: FreshdeskClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[] if request.url.path.endswith("tickets") else {}))))
    async with create_connected_server_and_client_session(server) as session:
        assert await demo.run_demo(session) == 1
    output = capsys.readouterr().out
    assert "no_demo_ticket" in output
    assert KEY not in output


def test_exact_mcp_pin():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert "mcp==1.30.0" in project["project"]["dependencies"]
    assert importlib.metadata.version("mcp") == "1.30.0"


async def test_demo_actual_stdio_with_mocked_http(capsys):
    # A real child process and SDK transport, but no live HTTP or credentials.
    child = """
import httpx
from freshdesk_mcp import FreshdeskClient
from freshdesk_mcp.server import create_server
ticket = {"id": 7, "subject": "FICTIONAL stdio", "status": 2, "priority": 1}
def handler(request):
    if request.url.path.endswith("search/tickets"):
        return httpx.Response(200, json={"total": 1, "results": [ticket]})
    if request.url.path.endswith("/7"):
        return httpx.Response(200, json=ticket)
    return httpx.Response(200, json=[ticket])
create_server(client_factory=lambda: FreshdeskClient(transport=httpx.MockTransport(handler))).run(transport="stdio")
"""
    parameters = StdioServerParameters(command=sys.executable, args=["-c", child],
        cwd=str(Path(__file__).parents[1]), env={
            "FRESHDESK_DOMAIN": "fictional-support.freshdesk.com",
            "FRESHDESK_API_KEY": KEY})
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            assert await demo.run_demo(session) == 0
    output = capsys.readouterr().out
    assert KEY not in output
    assert {json.loads(line)["tool"] for line in output.splitlines()} == {
        "list_tickets", "get_ticket", "search_tickets"}


async def test_seed_invalid_domain_makes_no_request(monkeypatch, runtime):
    monkeypatch.setenv("FRESHDESK_ALLOW_SEED", "1")
    monkeypatch.setenv("FRESHDESK_DOMAIN", "example.com")
    calls = []
    with pytest.raises(FreshdeskError) as exc:
        await seed_tickets.seed_tickets(transport=httpx.MockTransport(lambda request: calls.append(request)), sleep=runtime.sleep)
    assert exc.value.to_dict()["error_code"] == "configuration_error"
    assert not calls


def test_demo_main_missing_key_selects_mock(monkeypatch, capsys):
    monkeypatch.delenv("FRESHDESK_API_KEY")
    calls = []

    async def mock_demo():
        calls.append(True)
        return 0

    monkeypatch.setattr(demo, "start_mock_demo", mock_demo)
    assert demo.main([]) == 0
    assert calls == [True]


def test_demo_mock_flag_overrides_credentials(monkeypatch):
    async def mock_demo():
        return 0

    async def forbidden_real_demo():
        raise AssertionError("Real-account path must not be selected")

    monkeypatch.setattr(demo, "start_mock_demo", mock_demo)
    monkeypatch.setattr(demo, "start_demo", forbidden_real_demo)
    assert demo.main(["--mock"]) == 0


def test_demo_real_path_still_validates_domain(monkeypatch, capsys):
    monkeypatch.setenv("FRESHDESK_DOMAIN", "http://127.0.0.1")
    assert demo.main([]) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "configuration_error"


async def test_demo_mock_end_to_end_without_credentials(monkeypatch, capsys):
    monkeypatch.delenv("FRESHDESK_API_KEY")
    monkeypatch.delenv("FRESHDESK_DOMAIN")
    assert await demo.start_mock_demo() == 0
    output = capsys.readouterr().out
    lines = output.splitlines()
    assert lines[0] == "MOCK MODE: fictional data, no live Freshdesk account"
    records = [json.loads(line) for line in lines[1:]]
    assert records[-1]["mode"] == lines[0]
    assert records[-1]["status"] == "complete"
    assert {item.get("tool") for item in records} >= {"list_tickets", "get_ticket", "search_tickets"}
    assert any(item.get("event") == "incomplete" and item["has_more"] for item in records)
    assert all(item["has_more"] or item["truncated"] for item in records if item.get("event") == "incomplete")
    lists = [item["result"] for item in records if item.get("tool") == "list_tickets" and "result" in item]
    assert not lists[1]["has_more"] and not lists[1]["truncated"] and lists[1]["text_truncated"]
    retry = next(item for item in records if item.get("event") == "retry")
    assert retry == {"event": "retry", "scenario": "429", "retry_after_seconds": 1,
        "recorded_wait_seconds": 1, "successful_retry": True}
    ticket = next(item["result"] for item in records if item.get("tool") == "get_ticket")
    assert ticket["conversations"][0]["customer_provided"]["body"]
    html = next(item for item in records if item.get("event") == "expected_error")
    assert html["result"]["error_code"] == "invalid_response"
    assert KEY not in output
    assert "fictional-mock-key" not in output
    description = next(ticket for ticket in lists[1]["tickets"] if ticket["id"] == 8)["customer_provided"]["description"]
    assert "data. Fictional" in description
    assert "data.Fictional" not in description


async def test_demo_mock_transport_is_closed_and_exercises_retries(runtime):
    transport = demo.build_mock_transport()
    async with FreshdeskClient(transport=transport, sleep=runtime.sleep,
                               clock=lambda: runtime.now, random_source=lambda: 0) as client:
        await client.startup_check()
        first, has_more = await client.list_tickets_page(per_page=5, include_description=True)
        assert len(first) == 5 and has_more
        second, has_more = await client.list_tickets_page(page=2, per_page=5)
        assert len(second) == 5 and not has_more
        ticket = await client.get_ticket(1, include_conversations=True)
        assert ticket["conversations"]
        eighth = await client.get_ticket(8)
        assert eighth["description_text"] == " ".join(["Fictional customer-provided test data."] * 5)
        found, total = await client.search_tickets("tag:'fictional-demo'")
        assert len(found) == total == 10
        assert runtime.waits == [1]
        with pytest.raises(FreshdeskError) as exc:
            await client.get_ticket(999)
        assert exc.value.to_dict()["error_code"] == "invalid_response"
        with pytest.raises(FreshdeskError) as exc:
            await client.get_ticket(123456)
        assert exc.value.to_dict()["error_code"] == "not_found"
    async with httpx.AsyncClient(transport=demo.build_mock_transport()) as client:
        response = await client.get("https://example.com/unexpected")
        assert response.status_code == 404
        response = await client.get("https://fictional-support.freshdesk.com/api/v2/tickets/1/conversations")
        assert response.json()[0]["id"] == 101


def test_demo_redacts_json_escaped_credentials(monkeypatch, capsys):
    key = 'fictional-"key\\value'
    monkeypatch.setenv("FRESHDESK_API_KEY", key)
    demo.emit({"customer_provided": {"description": key}})
    output = capsys.readouterr().out
    assert key not in json.loads(output)["customer_provided"]["description"]
    assert json.dumps(key)[1:-1] not in output


def test_mock_honors_configured_deadline(monkeypatch, capsys):
    monkeypatch.setenv("FRESHDESK_TOTAL_TIMEOUT_SECONDS", "0.5")
    assert demo.main(["--mock"]) == 1
    output = capsys.readouterr().out
    records = [json.loads(line) for line in output.splitlines()[1:]]
    assert any(item.get("result", {}).get("error_code") == "deadline_exceeded" for item in records)
    assert any(item.get("event") == "summary" and item["status"] == "failed" for item in records)
    assert KEY not in output
