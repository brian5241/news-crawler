"""Playwright 브라우저 페칭 모듈 테스트 (오프라인, 네트워크 없이)."""
from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import pytest

from crawler.browser import BrowserFetcher, _check_playwright


class FakePage:
    def __init__(self, html="<html><body><p>hello</p></body></html>", url="https://example.com/article/1"):
        self._html = html
        self.url = url
        self._resp = MagicMock()
        self._resp.status = 200

    def goto(self, url, **kw):
        self.url = url
        return self._resp

    def wait_for_timeout(self, ms):
        pass

    def content(self):
        return self._html

    def close(self):
        pass


class FakeContext:
    def __init__(self, page=None):
        self._page = page or FakePage()

    def new_page(self):
        return self._page

    def close(self):
        pass


class FakeBrowser:
    def __init__(self, context=None):
        self._ctx = context or FakeContext()

    def new_context(self, **kw):
        return self._ctx

    def close(self):
        pass


class FakeChromium:
    def __init__(self, browser=None):
        self._browser = browser or FakeBrowser()

    def launch(self, **kw):
        return self._browser


class FakePlaywright:
    def __init__(self, chromium=None):
        self.chromium = chromium or FakeChromium()

    def start(self):
        return self

    def stop(self):
        pass


def _make_fetcher(page=None):
    """BrowserFetcher 를 만들되 실제 Playwright 를 쓰지 않도록 내부를 교체한다."""
    bf = object.__new__(BrowserFetcher)
    bf.timeout_ms = 30000
    bf.gap = 0.0
    bf.headless = True
    bf._last = 0.0
    bf.requests = 0
    page = page or FakePage()
    ctx = FakeContext(page)
    bf._pw = FakePlaywright(FakeChromium(FakeBrowser(ctx)))
    bf._browser = bf._pw.chromium.launch()
    bf._context = ctx
    return bf


def test_fetch_ok():
    html = "<html><head><title>Test</title></head><body><p>본문입니다.</p></body></html>"
    page = FakePage(html=html, url="https://example.com/article/123")
    bf = _make_fetcher(page)
    try:
        res = bf.fetch("https://example.com/article/123")
        assert res.ok
        assert res.status == 200
        assert "본문입니다" in res.text
        assert res.final_url == "https://example.com/article/123"
        assert bf.requests == 1
    finally:
        bf.close()


def test_fetch_blocked():
    html = '<html><body><div id="px-captcha">please verify</div></body></html>'
    page = FakePage(html=html)
    page._resp.status = 403
    bf = _make_fetcher(page)
    try:
        res = bf.fetch("https://example.com/blocked")
        assert res.blocked is not None
    finally:
        bf.close()


def test_fetch_error():
    page = FakePage()
    page.goto = MagicMock(side_effect=TimeoutError("page load timeout"))
    bf = _make_fetcher(page)
    try:
        res = bf.fetch("https://example.com/timeout")
        assert not res.ok
        assert "TimeoutError" in res.error
    finally:
        bf.close()


def test_context_manager():
    bf = _make_fetcher()
    with bf:
        res = bf.fetch("https://example.com/test")
        assert res.ok
    assert bf._browser is None
    assert bf._pw is None


def test_request_count():
    bf = _make_fetcher()
    try:
        bf.fetch("https://example.com/1")
        bf.fetch("https://example.com/2")
        bf.fetch("https://example.com/3")
        assert bf.requests == 3
    finally:
        bf.close()
