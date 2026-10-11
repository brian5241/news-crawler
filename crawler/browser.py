"""Playwright 기반 JS 렌더링 페칭 + 스텔스.

httpx 로는 본문을 못 받는 사이트(XHR 로 기사를 불러오는 SPA)에 쓴다.
sites.yaml 에서 js_render: true 로 표시된 사이트만 이쪽을 탄다.

스텔스 처리:
  - navigator.webdriver = false  (봇 탐지의 가장 흔한 신호)
  - chrome.runtime 위장          (headless Chrome 구분용)
  - WebGL vendor/renderer 위장   (HeadlessChrome 판별용)
  - plugins/languages 위장       (headless 는 보통 0개)
  - permissions 위장              (Notification 상태를 'default' 로)
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .fetch import FetchResult, decode_html, detect_block

log = logging.getLogger(__name__)

_PLAYWRIGHT_AVAILABLE: Optional[bool] = None

_CONTENT_SELECTORS = [
    "article",
    "[class*='article_body']", "[class*='article-body']", "[class*='articleBody']",
    "[class*='news_body']", "[class*='news-body']", "[class*='newsBody']",
    "[class*='story_body']", "[class*='story-body']",
    "[class*='cont_body']", "[class*='cont-body']",
    "[class*='view_cont']", "[class*='view-cont']",
    "[id*='articleBody']", "[id*='article-body']", "[id*='article_body']",
    "[id*='newsBody']", "[id*='news-body']", "[id*='news_body']",
    ".article_cont_area",
]
_SELECTOR_JOIN = ", ".join(_CONTENT_SELECTORS)

# 페이지가 열리기 전에 주입하는 스텔스 스크립트 (IIFE 로 즉시 실행)
_STEALTH_JS = """\
(() => {
    // 1. navigator.webdriver 제거 — 봇 탐지의 가장 기본적인 체크
    Object.defineProperty(navigator, 'webdriver', { get: () => false });

    // 2. chrome.runtime 위장 — headless 에는 비어 있어서 탐지됨
    window.chrome = window.chrome || {};
    window.chrome.runtime = window.chrome.runtime || {};
    window.chrome.csi = window.chrome.csi || function(){};
    window.chrome.loadTimes = window.chrome.loadTimes || function(){};

    // 3. Permissions API 위장 — headless 는 Notification 이 'denied'
    if (navigator.permissions) {
        const origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = (params) =>
            params.name === 'notifications'
                ? Promise.resolve({ state: 'default', onchange: null })
                : origQuery(params);
    }

    // 4. plugins 위장 — headless 는 plugins.length === 0
    const fakePlugins = {
        0: { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1 },
        1: { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '', length: 1 },
        2: { name: 'Native Client', filename: 'internal-nacl-plugin', description: '', length: 2 },
        length: 3,
        item: function(i) { return this[i] || null; },
        namedItem: function(n) { for (let i=0;i<this.length;i++) if(this[i].name===n) return this[i]; return null; },
        refresh: function(){},
    };
    Object.defineProperty(navigator, 'plugins', { get: () => fakePlugins });

    // 5. languages 위장
    Object.defineProperty(navigator, 'languages', { get: () => ['ko-KR', 'ko', 'en-US', 'en'] });

    // 6. WebGL vendor/renderer 위장
    try {
        const getParam = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(p) {
            if (p === 37445) return 'Google Inc. (Google)';
            if (p === 37446) return 'ANGLE (Google, Vulkan 1.3.0, SwiftShader)';
            return getParam.call(this, p);
        };
    } catch(e) {}

    // 7. Headless Chrome 의 빈 connection info 위장
    if (!navigator.connection) {
        Object.defineProperty(navigator, 'connection', {
            get: () => ({ effectiveType: '4g', rtt: 50, downlink: 10, saveData: false }),
        });
    }
})();
"""


def _check_playwright() -> bool:
    global _PLAYWRIGHT_AVAILABLE
    if _PLAYWRIGHT_AVAILABLE is None:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
            _PLAYWRIGHT_AVAILABLE = True
        except ImportError:
            _PLAYWRIGHT_AVAILABLE = False
    return _PLAYWRIGHT_AVAILABLE


class BrowserFetcher:
    def __init__(self, *, timeout: float = 30.0, gap: float = 1.0, headless: bool = True):
        if not _check_playwright():
            raise RuntimeError("playwright 가 설치되지 않았습니다: pip install playwright && playwright install chromium")
        self.timeout_ms = int(timeout * 1000)
        self.gap = gap
        self.headless = headless
        self._pw = None
        self._browser = None
        self._context = None
        self._last = 0.0
        self.requests = 0

    def _ensure_browser(self):
        if self._browser is not None:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
            ],
        )
        self._context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="ko-KR",
            viewport={"width": 1280, "height": 800},
            java_script_enabled=True,
        )
        self._context.add_init_script(_STEALTH_JS)

    def close(self):
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _wait_for_content(self, page) -> None:
        """기사 본문 영역이 렌더링될 때까지 기다린다. 못 찾으면 고정 대기로 폴백."""
        try:
            page.wait_for_selector(_SELECTOR_JOIN, timeout=8000)
            page.wait_for_timeout(500)
        except Exception:
            page.wait_for_timeout(3000)

    def fetch(self, url: str, *, wait_selector: Optional[str] = None) -> FetchResult:
        wait = self.gap - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.requests += 1
        started = time.monotonic()

        try:
            self._ensure_browser()
            page = self._context.new_page()
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                status = resp.status if resp else 0
                if wait_selector:
                    try:
                        page.wait_for_selector(wait_selector, timeout=10000)
                        page.wait_for_timeout(500)
                    except Exception:
                        page.wait_for_timeout(3000)
                else:
                    self._wait_for_content(page)
                html = page.content()
                final_url = page.url
            finally:
                page.close()

            elapsed = time.monotonic() - started
            text, enc = decode_html(html.encode("utf-8"), "utf-8")
            blocked, hard = detect_block(status, text[:30000])
            error = None if status < 400 else f"HTTP {status}"

            return FetchResult(
                url=url, final_url=final_url, status=status,
                content=html.encode("utf-8"), text=text, encoding=enc,
                content_type="text/html", error=error, elapsed=elapsed,
                blocked=blocked, block_hard=hard,
            )
        except Exception as exc:
            elapsed = time.monotonic() - started
            err_msg = f"{type(exc).__name__}: {exc}"[:200]
            log.debug("browser fetch error %s: %s", url, err_msg)
            return FetchResult(url=url, final_url=url, error=err_msg, elapsed=elapsed)
