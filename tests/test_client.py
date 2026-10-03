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
        assert await client.list_tickets(include_description=True) == [{"id": 1, "description": "Fictional ticket"}]


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
        assert await client.list_tickets(include_description=True) == [{"id": 1}, {"id": 2}]
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


@pytest.mark.parametrize("domain", ["http://fictional.example", "https://key@fictional.example", "https://fictional.example/?key=test", "https://fictional.example/path"])
def test_invalid_domain_is_sanitized(monkeypatch, domain):
    monkeypatch.setenv("FRESHDESK_DOMAIN", domain)
    with pytest.raises(FreshdeskError) as exc:
        FreshdeskClient()
    assert exc.value.to_dict()["error_code"] == "configuration_error"
    assert domain not in str(exc.value)


def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("FRESHDESK_API_KEY")
    with pytest.raises(FreshdeskError) as exc:
        FreshdeskClient()
    assert exc.value.to_dict()["error_code"] == "configuration_error"
