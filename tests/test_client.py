import base64

import httpx
import pytest

from freshdesk_mcp import FreshdeskClient, FreshdeskError


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("FRESHDESK_DOMAIN", "fictional-support.freshdesk.com")
    monkeypatch.setenv("FRESHDESK_API_KEY", "fictional-test-key")


@pytest.mark.asyncio
async def test_success_auth_timeout_and_description():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/api/v2/tickets"
        assert dict(request.url.params) == {"page": "1", "per_page": "30", "include": "description"}
        expected = base64.b64encode(b"fictional-test-key:X").decode()
        assert request.headers["authorization"] == f"Basic {expected}"
        assert request.extensions["timeout"]["read"] == 7
        return httpx.Response(200, json=[{"id": 1, "description": "Fictional ticket"}])

    async with FreshdeskClient(timeout_seconds=7, transport=httpx.MockTransport(handler)) as client:
        assert await client.list_tickets(include_description=True) == ([{"id": 1, "description": "Fictional ticket"}], False)


@pytest.mark.asyncio
async def test_pagination_uses_link_and_preserves_include():
    pages = []

    def handler(request):
        page = int(request.url.params["page"])
        pages.append(page)
        assert request.url.params["include"] == "description"
        headers = {"Link": '<https://fictional-support.freshdesk.com/api/v2/tickets?page=2>; rel="next"'} if page == 1 else {}
        return httpx.Response(200, json=[{"id": page}], headers=headers)

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        assert await client.list_tickets(include_description=True) == ([{"id": 1}, {"id": 2}], False)
    assert pages == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(401, "authentication_failed"), (403, "access_denied"), (404, "not_found")])
async def test_startup_errors_are_sanitized_without_retry(status, code, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="fictional-test-key sensitive upstream content")

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FreshdeskError) as exc:
            await client.startup_check()
    error = exc.value.to_dict()
    assert error["error_code"] == code
    assert error["retryable"] is False
    assert error["retry_after_seconds"] is None
    assert set(error) == {"error_code", "message", "retryable", "retry_after_seconds"}
    assert "fictional-test-key" not in str(exc.value)
    assert "fictional-test-key" not in repr(exc.value)
    assert "sensitive upstream" not in str(exc.value)
    assert "fictional-test-key" not in caplog.text
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_startup_success():
    def handler(request):
        assert request.url.params["per_page"] == "1"
        assert "include" not in request.url.params
        return httpx.Response(200, json=[])

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        assert await client.startup_check() is None


@pytest.mark.asyncio
async def test_timeout_is_sanitized():
    def handler(request):
        raise httpx.ReadTimeout("fictional-test-key", request=request)

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FreshdeskError) as exc:
            await client.list_tickets()
    assert exc.value.to_dict()["error_code"] == "timeout"
    assert "fictional-test-key" not in str(exc.value)
    assert "fictional-test-key" not in repr(exc.value)


@pytest.mark.parametrize("domain", ["http://fictional.example", "https://key@fictional.example", "https://fictional.example/?key=test", "https://fictional.example/path"])
def test_invalid_domain_is_sanitized(monkeypatch, domain):
    monkeypatch.setenv("FRESHDESK_DOMAIN", domain)
    with pytest.raises(FreshdeskError) as exc:
        FreshdeskClient()
    assert exc.value.to_dict()["error_code"] == "configuration_error"
    assert domain not in str(exc.value)
    assert "fictional-test-key" not in str(exc.value)
    assert "fictional-test-key" not in repr(exc.value)


def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("FRESHDESK_API_KEY")
    with pytest.raises(FreshdeskError) as exc:
        FreshdeskClient()
    assert exc.value.to_dict()["error_code"] == "configuration_error"
    assert "fictional-test-key" not in str(exc.value)
    assert "fictional-test-key" not in repr(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["fictional-test-key not JSON", '{"fictional-test-key":', '{"fictional-test-key": 1}', '["fictional-test-key"]'])
async def test_invalid_response_is_sanitized(body):
    async with FreshdeskClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))) as client:
        with pytest.raises(FreshdeskError) as exc:
            await client.list_tickets()
    assert exc.value.to_dict()["error_code"] == "invalid_response"
    assert "fictional-test-key" not in str(exc.value)
    assert "fictional-test-key" not in repr(exc.value)


@pytest.mark.asyncio
async def test_connection_error_is_sanitized():
    def handler(request):
        raise httpx.ConnectError("fictional-test-key", request=request)

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FreshdeskError) as exc:
            await client.list_tickets()
    assert exc.value.to_dict()["error_code"] == "connection_error"
    assert "fictional-test-key" not in str(exc.value)
    assert "fictional-test-key" not in repr(exc.value)
    assert exc.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(429, "rate_limited"), (500, "http_error"), (400, "http_error")])
async def test_other_http_errors_are_sanitized(status, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="fictional-test-key")

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FreshdeskError) as exc:
            await client.list_tickets()
    assert exc.value.to_dict()["error_code"] == code
    assert "fictional-test-key" not in str(exc.value)
    assert "fictional-test-key" not in repr(exc.value)
    assert len(calls) == (4 if status in (429, 500) else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("argument", ["per_page", "page"])
async def test_pagination_limits_before_request(argument):
    from freshdesk_mcp.client import MAX_PAGE, MAX_PER_PAGE

    calls = []
    async with FreshdeskClient(transport=httpx.MockTransport(lambda request: calls.append(request))) as client:
        with pytest.raises(FreshdeskError) as exc:
            await client.list_tickets(**{argument: (MAX_PER_PAGE if argument == "per_page" else MAX_PAGE) + 1})
    assert exc.value.to_dict()["error_code"] == "invalid_argument"
    assert "fictional-test-key" not in str(exc.value)
    assert "fictional-test-key" not in repr(exc.value)
    assert calls == []


@pytest.mark.asyncio
async def test_no_next_link_stops_even_with_full_page():
    from freshdesk_mcp.client import MAX_PER_PAGE

    calls = []
    tickets = [{"id": ticket_id} for ticket_id in range(MAX_PER_PAGE)]

    def handler(request):
        calls.append(request)
        assert "include" not in request.url.params
        return httpx.Response(200, json=tickets)

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        assert await client.list_tickets(per_page=MAX_PER_PAGE) == (tickets, False)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("has_next", [True, False])
async def test_last_allowed_page_returns_collected_tickets(has_next):
    from freshdesk_mcp.client import MAX_PAGE

    pages = []

    def handler(request):
        page = int(request.url.params["page"])
        pages.append(page)
        headers = {"Link": f'<https://fictional-support.freshdesk.com/api/v2/tickets?page={page + 1}>; rel="next"'} if page < MAX_PAGE or has_next else {}
        return httpx.Response(200, json=[{"id": page}], headers=headers)

    async with FreshdeskClient(transport=httpx.MockTransport(handler)) as client:
        assert await client.list_tickets(page=MAX_PAGE - 1) == ([{"id": MAX_PAGE - 1}, {"id": MAX_PAGE}], has_next)
    assert pages == [MAX_PAGE - 1, MAX_PAGE]


@pytest.mark.parametrize("domain", ["fictional-test-key.example.com", "https://fictional-test-key.freshdesk.com.example.com", "https://freshdesk.com", "http://fictional-test-key.freshdesk.com"])
def test_non_freshdesk_domain_makes_no_request(monkeypatch, domain):
    monkeypatch.setenv("FRESHDESK_DOMAIN", domain)
    calls = []
    with pytest.raises(FreshdeskError) as exc:
        FreshdeskClient(transport=httpx.MockTransport(lambda request: calls.append(request)))
    assert exc.value.to_dict()["error_code"] == "configuration_error"
    assert "fictional-test-key" not in str(exc.value)
    assert "fictional-test-key" not in repr(exc.value)
    assert calls == []


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout_is_sanitized(timeout):
    with pytest.raises(FreshdeskError) as exc:
        FreshdeskClient(timeout_seconds=timeout)
    assert exc.value.to_dict()["error_code"] == "configuration_error"
    assert "fictional-test-key" not in str(exc.value)
    assert "fictional-test-key" not in repr(exc.value)
