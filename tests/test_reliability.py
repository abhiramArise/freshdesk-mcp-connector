import asyncio

import httpx
import pytest

from freshdesk_mcp import FreshdeskClient, FreshdeskError


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("FRESHDESK_DOMAIN", "fictional-support.freshdesk.com")
    monkeypatch.setenv("FRESHDESK_API_KEY", "fictional-test-key")


@pytest.mark.parametrize("header,expected", [("12", 12.1), (None, 1.1), ("invalid", 1.1), ("1.5", 1.1), ("-1", 1.1), ("999", 60), ("60", 60), ("0", 0.1)])
async def test_retry_after_then_success(header, expected, runtime, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        assert "fictional-test-key" not in str(request.url)
        if len(calls) == 1:
            return httpx.Response(429, headers={} if header is None else {"Retry-After": header}, text="fictional-test-key")
        return httpx.Response(200, json=[])

    async with FreshdeskClient(transport=httpx.MockTransport(handler), random_source=lambda: 0.5) as client:
        assert await client.list_tickets() == ([], False)
    assert len(calls) == 2
    assert runtime.waits == pytest.approx([expected])
    assert "fictional-test-key" not in caplog.text


@pytest.mark.parametrize("kind,code", [(429, "rate_limited"), (500, "http_error"), (503, "http_error"), ("timeout", "timeout"), ("connection", "connection_error")])
async def test_exhaustion_sanitized(kind, code, runtime, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        if kind == "timeout":
            raise httpx.ReadTimeout("fictional-test-key", request=request)
        if kind == "connection":
            raise httpx.ConnectError("fictional-test-key", request=request)
        return httpx.Response(kind, text="fictional-test-key", headers={"Retry-After": "fictional-test-key"})

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FreshdeskError) as exc:
            await client.list_tickets()
    assert len(calls) == 4
    assert runtime.waits == [1, 2, 4]
    assert exc.value.to_dict() == {"error_code": code, "message": str(exc.value), "retryable": True, "retry_after_seconds": 8}
    assert "fictional-test-key" not in str(exc.value)
    assert "fictional-test-key" not in repr(exc.value)
    assert "fictional-test-key" not in repr(exc.value.to_dict())
    assert "fictional-test-key" not in caplog.text
    assert exc.value.__context__ is None


async def test_exhausted_retry_after_is_set_and_capped(runtime):
    async with FreshdeskClient(transport=httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": "1000"}))) as client:
        with pytest.raises(FreshdeskError) as exc:
            await client.list_tickets()
    assert runtime.waits == [60, 60, 60]
    assert exc.value.to_dict()["retry_after_seconds"] == 60


@pytest.mark.parametrize("kind", [500, 502, "timeout", "connection"])
async def test_transient_failure_then_success(kind, runtime, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            if kind == "timeout":
                raise httpx.ReadTimeout("fictional-test-key", request=request)
            if kind == "connection":
                raise httpx.ConnectError("fictional-test-key", request=request)
            return httpx.Response(kind, text="fictional-test-key")
        return httpx.Response(200, json=[])

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        assert await client.list_tickets() == ([], False)
    assert len(calls) == 2
    assert runtime.waits == [1]
    assert "fictional-test-key" not in caplog.text


@pytest.mark.parametrize("status", [400, 401, 403, 404, 408, 422])
async def test_4xx_not_retried(status, runtime, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="fictional-test-key")

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FreshdeskError) as exc:
            await client.list_tickets()
    assert len(calls) == 1
    assert runtime.waits == []
    assert not exc.value.to_dict()["retryable"]
    assert "fictional-test-key" not in str(exc.value)
    assert "fictional-test-key" not in repr(exc.value)
    assert "fictional-test-key" not in caplog.text


async def test_sliding_window_includes_retries(monkeypatch, runtime):
    monkeypatch.setenv("FRESHDESK_CALLS_PER_MINUTE", "2")
    times = []

    def handler(request):
        times.append(runtime.now)
        return httpx.Response(500 if len(times) <= 2 else 200, json=[])

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        await client.list_tickets()
        await client.list_tickets()
    assert times == [0, 1, 60, 61]
    assert runtime.waits == [1, 59, 1]


async def test_concurrent_calls_share_limiter(monkeypatch, runtime):
    monkeypatch.setenv("FRESHDESK_CALLS_PER_MINUTE", "1")
    times = []

    def handler(request):
        times.append(runtime.now)
        return httpx.Response(200, json=[])

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        await asyncio.gather(client.list_tickets(), client.list_tickets(), client.list_tickets())
    assert times == [0, 60, 120]


@pytest.mark.parametrize("remaining,total,expected", [("5.0", "50.0", 12), ("0", "50", 60), ("6", "50", 0), ("bad", "50", 0), ("NaN", "50", 0), ("1", "0", 0), ("-1", "50", 0)])
async def test_low_budget_headers(remaining, total, expected, runtime):
    times = []

    def handler(request):
        times.append(runtime.now)
        return httpx.Response(200, json=[], headers={"X-RateLimit-Remaining": remaining, "X-RateLimit-Total": total})

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        await client.list_tickets()
        await client.list_tickets()
    assert times == [0, expected]


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "fictional-test-key"])
def test_invalid_limiter_configuration(monkeypatch, value):
    monkeypatch.setenv("FRESHDESK_CALLS_PER_MINUTE", value)
    with pytest.raises(FreshdeskError) as exc:
        FreshdeskClient()
    assert exc.value.to_dict()["error_code"] == "configuration_error"
    assert "fictional-test-key" not in str(exc.value)
    assert "fictional-test-key" not in repr(exc.value)


async def test_default_limiter(monkeypatch, runtime):
    monkeypatch.delenv("FRESHDESK_CALLS_PER_MINUTE")
    async with FreshdeskClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))) as client:
        for _ in range(31):
            await client.list_tickets()
    assert runtime.waits == [60]


async def test_exhaustion_preserves_cooldown_for_next_call(runtime):
    times = []

    def handler(request):
        times.append(runtime.now)
        return httpx.Response(429, headers={"Retry-After": "10"}) if len(times) <= 4 else httpx.Response(200, json=[])

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FreshdeskError):
            await client.list_tickets()
        assert runtime.waits == [10, 10, 10]
        assert await client.list_tickets() == ([], False)
    assert times == [0, 10, 20, 30, 40]


async def test_cancellation_is_not_retried(runtime):
    calls = []

    def handler(request):
        calls.append(request)
        raise asyncio.CancelledError()

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(asyncio.CancelledError):
            await client.list_tickets()
    assert len(calls) == 1
    assert runtime.waits == []


async def test_invalid_body_after_retry_is_not_retried(runtime, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500 if len(calls) == 1 else 200, text="fictional-test-key")

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FreshdeskError) as exc:
            await client.list_tickets()
    assert len(calls) == 2
    assert runtime.waits == [1]
    assert exc.value.to_dict()["error_code"] == "invalid_response"
    assert "fictional-test-key" not in str(exc.value)
    assert "fictional-test-key" not in repr(exc.value)
    assert "fictional-test-key" not in caplog.text
