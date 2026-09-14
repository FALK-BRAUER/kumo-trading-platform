"""Offline tests for the FMP REST client's error handling. No network: `_session.get` is stubbed.

The `FmpHttpError` tests are a security regression guard, not a nicety: FMP puts the API key in the query
string (`apikey=...`), and most `aiohttp` exceptions (`ClientResponseError` from `raise_for_status()`,
`ContentTypeError` from `resp.json()`, `TooManyRedirects`, ...) embed the full request URL — key included
— in their own `str()`. Confirmed live: this leaked a real key into engine logs via a plain
`_log.warning("...: %s", exc)` call. Round 1 of the fix only guarded the HTTP-error-status branch; codex
review round 2 found the `resp.json()` decode path and a hypothetical key-echoing response body were both
still open. `_get` now wraps the ENTIRE request in one boundary that never lets an upstream exception's own
message (or an unredacted response body) cross into `FmpHttpError`.
"""

from __future__ import annotations

import asyncio

from api.providers.fmp.http import FmpHttpClient, FmpHttpError

_FAKE_KEY = "super-secret-fmp-key-do-not-leak"


class _FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RaisingResponse:
    """Simulates a transport-level failure surfacing mid-request (e.g. aiohttp.ContentTypeError,
    TooManyRedirects) — `text()` raises instead of returning a body. The exception's message deliberately
    embeds the key + full query string, mimicking real aiohttp exceptions that include `request_info`."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def text(self):
        raise self._exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        self.last_call: tuple[str, dict] | None = None
        self._raise: Exception | None = None

    def get(self, url: str, params: dict):
        self.last_call = (url, params)
        if self._raise is not None:
            return _RaisingResponse(self._raise)
        return _FakeResponse(self.status, self.body)


def _client_with(status: int, body: str) -> tuple[FmpHttpClient, _FakeSession]:
    client = FmpHttpClient(_FAKE_KEY)
    fake = _FakeSession(status, body)
    client._session = fake  # bypass connect() — no real aiohttp.ClientSession needed offline
    return client, fake


def _client_raising(exc: Exception) -> FmpHttpClient:
    client = FmpHttpClient(_FAKE_KEY)
    fake = _FakeSession(200, "")
    fake._raise = exc
    client._session = fake
    return client


def test_error_status_raises_fmp_http_error_not_client_response_error():
    client, _ = _client_with(402, '{"Error Message": "payment required"}')
    try:
        asyncio.run(client.get_quote("WDAY"))
        assert False, "expected FmpHttpError"
    except FmpHttpError as exc:
        assert exc.status == 402
        assert exc.symbol == "WDAY"


def test_fmp_http_error_message_never_contains_the_api_key():
    client, _ = _client_with(402, '{"Error Message": "payment required"}')
    try:
        asyncio.run(client.get_quote("WDAY"))
        assert False, "expected FmpHttpError"
    except FmpHttpError as exc:
        assert _FAKE_KEY not in str(exc)


def test_fmp_http_error_redacts_the_api_key_even_when_the_body_echoes_it():
    # A pathological/misbehaving upstream (WAF, proxy error page) that echoes the request back in its
    # error body must not leak the key either — _redact() strips it before it ever reaches FmpHttpError.
    client, _ = _client_with(402, f"echo: apikey={_FAKE_KEY}")
    try:
        asyncio.run(client.get_quote("WDAY"))
        assert False, "expected FmpHttpError"
    except FmpHttpError as exc:
        assert _FAKE_KEY not in str(exc)
        assert "REDACTED" in str(exc)  # body content is preserved, just with the key scrubbed out


def test_non_status_exception_during_the_request_never_leaks_str_of_the_original():
    # Simulates aiohttp.ContentTypeError / TooManyRedirects / a connection failure — anything that isn't
    # the resp.status >= 400 branch. The original exception's OWN message (which real aiohttp exceptions
    # often build from `request_info.real_url`, key included) must never cross into FmpHttpError.
    leaky = RuntimeError(f"simulated aiohttp failure, real_url=https://x/?apikey={_FAKE_KEY}")
    client = _client_raising(leaky)
    try:
        asyncio.run(client.get_quote("AAPL"))
        assert False, "expected FmpHttpError"
    except FmpHttpError as exc:
        assert _FAKE_KEY not in str(exc)
        assert "RuntimeError" in str(exc)  # the exception TYPE name is fine to surface, just not its message
        assert exc.status == 0  # sentinel: not an HTTP status, a transport-level failure


def test_json_decode_failure_does_not_use_resp_json_content_type_error_path():
    # A 2xx response with a non-JSON body would make aiohttp's own resp.json() raise ContentTypeError
    # (which embeds the URL). _get uses resp.text() + json.loads() instead, so this surfaces as a plain
    # JSONDecodeError caught by the generic handler — never aiohttp's URL-bearing exception type.
    client, _ = _client_with(200, "not json at all")
    try:
        asyncio.run(client.get_quote("AAPL"))
        assert False, "expected FmpHttpError"
    except FmpHttpError as exc:
        assert _FAKE_KEY not in str(exc)


def test_get_quote_requests_the_right_symbol_and_path():
    client, fake = _client_with(200, '[{"price": 1.0}]')
    result = asyncio.run(client.get_quote("AAPL"))
    assert result == {"price": 1.0}
    url, params = fake.last_call
    assert url.endswith("/quote")
    assert params["symbol"] == "AAPL"
    assert params["apikey"] == _FAKE_KEY  # the key IS sent — just never LOGGED


def test_get_quote_empty_list_is_none_not_an_error():
    client, _ = _client_with(200, "[]")
    assert asyncio.run(client.get_quote("UNKNOWN")) is None


def test_get_annual_eps_reads_the_eps_field():
    client, _ = _client_with(200, '[{"eps": 8.23}]')
    assert asyncio.run(client.get_annual_eps("AAPL")) == 8.23


def test_get_annual_eps_missing_field_is_none():
    client, _ = _client_with(200, "[{}]")
    assert asyncio.run(client.get_annual_eps("AAPL")) is None
