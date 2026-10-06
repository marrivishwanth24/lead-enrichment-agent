"""Tests for scraper.py — mocked httpx, no real network calls."""

from unittest.mock import patch

import httpx
import pytest

from app.scraper import ScrapeError, fetch_website_text


class _FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class _FakeAsyncClient:
    def __init__(self, response=None, raise_error=None):
        self._response = response
        self._raise_error = raise_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url):
        if self._raise_error:
            raise self._raise_error
        return self._response


@pytest.mark.asyncio
async def test_fetch_website_text_strips_noise_tags():
    html = "<html><head><script>bad()</script></head><body><nav>Menu</nav><main><h1>Acme</h1><p>We build widgets.</p></main></body></html>"
    fake = _FakeAsyncClient(response=_FakeResponse(html))

    with patch("httpx.AsyncClient", lambda *a, **kw: fake):
        text = await fetch_website_text("acme.com")

    assert "Acme" in text
    assert "We build widgets" in text
    assert "Menu" not in text
    assert "bad()" not in text


@pytest.mark.asyncio
async def test_fetch_website_text_normalizes_bare_domain():
    html = "<html><body><p>content</p></body></html>"
    fake = _FakeAsyncClient(response=_FakeResponse(html))
    captured = {}

    async def _get(self, url):
        captured["url"] = url
        return _FakeResponse(html)

    with patch.object(_FakeAsyncClient, "get", _get), \
         patch("httpx.AsyncClient", lambda *a, **kw: fake):
        await fetch_website_text("acme.com")

    assert captured["url"] == "https://acme.com"


@pytest.mark.asyncio
async def test_fetch_website_text_raises_scrape_error_on_network_failure():
    fake = _FakeAsyncClient(raise_error=httpx.ConnectError("refused"))

    with patch("httpx.AsyncClient", lambda *a, **kw: fake):
        with pytest.raises(ScrapeError):
            await fetch_website_text("unreachable.example")


@pytest.mark.asyncio
async def test_fetch_website_text_raises_on_empty_page():
    fake = _FakeAsyncClient(response=_FakeResponse("<html><body></body></html>"))

    with patch("httpx.AsyncClient", lambda *a, **kw: fake):
        with pytest.raises(ScrapeError):
            await fetch_website_text("blank.example")


@pytest.mark.asyncio
async def test_fetch_website_text_truncates_long_pages():
    html = "<html><body><p>" + ("word " * 5000) + "</p></body></html>"
    fake = _FakeAsyncClient(response=_FakeResponse(html))

    with patch("httpx.AsyncClient", lambda *a, **kw: fake):
        text = await fetch_website_text("huge.example")

    assert len(text) <= 6000
