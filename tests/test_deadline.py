import logging

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from freshdesk_mcp import FreshdeskClient, FreshdeskError
from freshdesk_mcp.server import create_server

KEY = "fictional-test-key"


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("FRESHDESK_DOMAIN", "fictional-support.freshdesk.com")
    monkeypatch.setenv("FRESHDESK_API_KEY", KEY)
    monkeypatch.delenv("FRESHDESK_TOTAL_TIMEOUT_SECONDS", raising=False)


@pytest.mark.parametrize("kind", ["retry", "limiter", "low_budget"])
async def test_deadline_before_sleep(kind, monkeypatch, runtime, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("FRESHDESK_TOTAL_TIMEOUT_SECONDS", "0.5")
    calls = []
    if kind == "limiter":
        monkeypatch.setenv("FRESHDESK_CALLS_PER_MINUTE", "1")

    def handler(request):
        calls.append(request)
        headers = {"X-RateLimit-Remaining": "1", "X-RateLimit-Total": "50"} if kind == "low_budget" else {}
        return httpx.Response(500 if kind == "retry" else 200, json=[], headers=headers)

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        if kind != "retry":
            await client.list_tickets()
        with pytest.raises(FreshdeskError) as exc:
            await client.list_tickets()
    assert len(calls) == 1
    assert runtime.waits == []
    assert exc.value.to_dict()["error_code"] == "deadline_exceeded"
    assert exc.value.to_dict()["retryable"] is True
    assert exc.value.to_dict()["retry_after_seconds"] == (1 if kind == "retry" else 60)
    assert KEY not in str(exc.value) + repr(exc.value) + caplog.text


async def test_default_deadline_is_45_seconds(runtime):
    async with FreshdeskClient(transport=httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": "46"}))) as client:
        with pytest.raises(FreshdeskError) as exc:
            await client.list_tickets()
    assert runtime.waits == []
    assert exc.value.to_dict()["error_code"] == "deadline_exceeded"
    assert exc.value.to_dict()["retry_after_seconds"] == 46


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "inf", KEY])
def test_invalid_deadline_configuration(value, monkeypatch):
    monkeypatch.setenv("FRESHDESK_TOTAL_TIMEOUT_SECONDS", value)
    with pytest.raises(FreshdeskError) as exc:
        FreshdeskClient()
    assert exc.value.to_dict()["error_code"] == "configuration_error"
    assert KEY not in str(exc.value) + repr(exc.value)


async def test_network_timeout_shrinks_and_late_success_is_rejected(runtime, monkeypatch):
    monkeypatch.setenv("FRESHDESK_TOTAL_TIMEOUT_SECONDS", "3")
    timeouts = []

    def handler(request):
        timeouts.append(request.extensions["timeout"])
        runtime.now += 2
        return httpx.Response(500 if len(timeouts) == 1 else 200, json=[])

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FreshdeskError) as exc:
            await client.list_tickets()
    assert exc.value.to_dict()["error_code"] == "deadline_exceeded"
    assert timeouts == [dict(connect=3, read=3, write=3, pool=3)]
    assert runtime.waits == [1]


async def test_multi_page_list_shares_deadline(runtime, monkeypatch):
    monkeypatch.setenv("FRESHDESK_TOTAL_TIMEOUT_SECONDS", "5")
    timeouts = []

    def handler(request):
        timeouts.append(request.extensions["timeout"]["read"])
        runtime.now += 3
        return httpx.Response(200, json=[{"id": len(timeouts)}], headers={"Link": '<https://fictional-support.freshdesk.com/api/v2/tickets?page=2>; rel="next"'})

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FreshdeskError) as exc:
            await client.list_tickets()
    assert timeouts == [5, 2]
    assert exc.value.to_dict()["error_code"] == "deadline_exceeded"
    assert runtime.waits == []


@pytest.mark.parametrize("failure,retried", [(httpx.ReadTimeout, True), (httpx.ConnectError, True),
    (httpx.ReadError, True), (httpx.WriteError, True), (httpx.PoolTimeout, True),
    (httpx.RemoteProtocolError, True), (httpx.LocalProtocolError, False), (httpx.ProxyError, False),
    (httpx.UnsupportedProtocol, False), (httpx.DecodingError, False), (httpx.TooManyRedirects, False)])
async def test_only_selected_exceptions_retry(failure, retried, runtime, caplog):
    caplog.set_level(logging.DEBUG)
    calls = []

    def handler(request):
        calls.append(request)
        raise failure(KEY, request=request)

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FreshdeskError) as exc:
            await client.list_tickets()
    assert len(calls) == (4 if retried else 1)
    assert runtime.waits == ([1, 2, 4] if retried else [])
    assert exc.value.to_dict()["retryable"] is retried
    assert KEY not in str(exc.value) + repr(exc.value) + caplog.text


@pytest.mark.parametrize("name,args", [("list_tickets", {}), ("get_ticket", {"ticket_id": 7}),
    ("search_tickets", {"query": "status:2"})])
@pytest.mark.parametrize("header,code,wait", [("1", "deadline_exceeded", 1), ("61", "rate_limited", 61)])
async def test_deadline_error_survives_mcp_boundary(name, args, header, code, wait, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("FRESHDESK_TOTAL_TIMEOUT_SECONDS", "0.5")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[]) if len(calls) == 1 else httpx.Response(429, headers={"Retry-After": header}, text=KEY)

    server = create_server(client_factory=lambda: FreshdeskClient(transport=httpx.MockTransport(handler)))
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(name, args)
    assert result.isError
    assert result.structuredContent == {"error_code": code, "message": result.structuredContent["message"],
        "retryable": True, "retry_after_seconds": wait}
    assert len(calls) == 2
    assert KEY not in repr(result) + caplog.text


async def test_retry_timeout_uses_remaining_budget(runtime, monkeypatch):
    monkeypatch.setenv("FRESHDESK_TOTAL_TIMEOUT_SECONDS", "5")
    timeouts = []

    def handler(request):
        timeouts.append(request.extensions["timeout"])
        runtime.now += 1
        return httpx.Response(500 if len(timeouts) == 1 else 200, json=[])

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        assert await client.list_tickets() == ([], False)
    assert timeouts == [dict(connect=5, read=5, write=5, pool=5), dict(connect=3, read=3, write=3, pool=3)]
    assert runtime.waits == [1]


async def test_limiter_lock_wait_counts_toward_deadline(runtime):
    import asyncio

    calls = []
    async with FreshdeskClient(transport=httpx.MockTransport(lambda request: calls.append(request))) as client:
        await client._limiter_lock.acquire()
        task = asyncio.create_task(client.list_tickets())
        await asyncio.sleep(0)
        runtime.now = 46
        client._limiter_lock.release()
        with pytest.raises(FreshdeskError) as exc:
            await task
        assert not client._limiter_lock.locked()
    assert exc.value.to_dict()["error_code"] == "deadline_exceeded"
    assert not calls and not runtime.waits


async def test_jitter_is_included_in_refused_wait(runtime, monkeypatch):
    monkeypatch.setenv("FRESHDESK_TOTAL_TIMEOUT_SECONDS", "1.05")
    async with FreshdeskClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)), random_source=lambda: 0.5) as client:
        with pytest.raises(FreshdeskError) as exc:
            await client.list_tickets()
    assert exc.value.to_dict()["retry_after_seconds"] == pytest.approx(1.1)
    assert not runtime.waits
