"""기사 주소 수집(목록 단계).

sites.yaml 의 블록 하나(dict)를 받아 기사 URL 후보를 모은다. 우선순위:
  1. RSS 피드 항목 (가장 정확. 제목·발행시각이 같이 온다)
  2. 뉴스 사이트맵 <loc> (없으면 일반 사이트맵). 인덱스면 'news' 이름의 자식 → 뒤쪽(최근) 자식 순으로 최대 3개
  3. 목록 페이지·홈페이지 링크 중 기사 패턴에 맞는 것
각 단계에서 limit 만큼 모이면 멈춘다. 결과는 중복 제거된 [{url, title, published, source}].
"""
from __future__ import annotations

import html as htmlmod
import logging
import re
import time
import zlib
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin, urlsplit

import feedparser

from .discover import _EXCLUDE_RE, collect_links, longest_digit_run, normalize_url, registrable_domain
from .extract import _parse
from .fetch import FetchResult, fetch, make_client

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
FEED_MAX_BYTES = 2 * 1024 * 1024
SITEMAP_MAX_BYTES = 3 * 1024 * 1024
_URL_BLOCK_RE = re.compile(rb"<url>(.*?)</url>", re.S | re.I)
_LOC_RE = re.compile(rb"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_DATE_RE = re.compile(rb"<(?:news:publication_date|lastmod)>\s*([^<\s]+)\s*<", re.I)
_SITEMAP_CHILD_RE = re.compile(rb"<sitemap>(.*?)</sitemap>", re.S | re.I)
_TITLE_RE = re.compile(rb"<news:title>\s*(.*?)\s*</news:title>", re.S | re.I)
_NON_ARTICLE_PATH_RE = re.compile(r"(/timeline/|/info/|/company|/about|/main\d{4}|/intro|/guide|/policy|/privacy|/terms)", re.I)


def _gunzip_if_needed(content: bytes) -> bytes:
    if content[:2] == b"\x1f\x8b":
        try:  # 잘린 스트림도 받은 데까지는 풀린다
            return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(content)
        except zlib.error:
            return b""
    return content


def _struct_to_kst(t) -> Optional[str]:
    if not t:
        return None
    try:
        return datetime(*t[:6], tzinfo=timezone.utc).astimezone(KST).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None


class Collector:
    def __init__(self, client=None, *, gap: float = 0.5, timeout: float = 12.0):
        self.client = client or make_client(timeout)
        self._own = client is None
        self.gap = gap
        self.timeout = timeout
        self._last = 0.0
        self.requests = 0
        self.consecutive_errors = 0     # 네트워크 오류(응답 없음)가 연달아 난 횟수
        self.max_consecutive_errors = 2  # 이만큼 연달아 나면 그 사이트는 포기한다(죽은 주소를 계속 두드리지 않도록)
        self.blocked: Optional[str] = None   # 봇 차단·속도 제한 응답을 받았으면 이유
        self.retry_after: Optional[float] = None
        self.soft_403 = 0                    # 단순 403 연속 횟수. 특정 주소만 막힌 걸 수 있어 2회 연속일 때 차단으로 본다
        self.max_soft_403 = 2
        self.aborted = ""

    def close(self) -> None:
        if self._own:
            self.client.close()

    @property
    def tripped(self) -> bool:
        return self.blocked is not None or self.consecutive_errors >= self.max_consecutive_errors

    def _get(self, url: str, max_bytes: Optional[int] = None) -> FetchResult:
        if self.tripped:
            self.aborted = (f"봇 차단 응답으로 중단: {self.blocked}" if self.blocked
                            else f"네트워크 오류 {self.consecutive_errors}회 연속으로 중단")
            return FetchResult(url=url, final_url=url, error="circuit open: " + self.aborted)
        wait = self.gap - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.requests += 1
        kw = {"max_bytes": max_bytes} if max_bytes else {}
        res = fetch(url, client=self.client, timeout=self.timeout, retries=1, **kw)
        if res.blocked and (res.block_hard or self.soft_403 + 1 >= self.max_soft_403):
            # 봇 검사 페이지·429 는 즉시, 단순 403 은 연속 2회면 끊는다. 더 두드리면 IP 차단으로 번진다
            self.blocked = res.blocked if res.block_hard else f"{res.blocked} ×{self.soft_403 + 1} 연속"
            self.retry_after = res.retry_after
            self.aborted = f"봇 차단 응답으로 중단: {self.blocked}"
        elif res.blocked:
            self.soft_403 += 1
        elif res.status == 0:   # 연결·읽기 실패 (HTTP 오류 코드는 서버가 살아 있다는 뜻이라 세지 않는다)
            self.consecutive_errors += 1
        else:
            self.consecutive_errors = 0
            self.soft_403 = 0
        return res

    def get(self, url: str, max_bytes: Optional[int] = None) -> FetchResult:
        """같은 사이트 요청 간격을 지키는 공개 GET (파이프라인이 기사 본문을 받을 때 쓴다)."""
        return self._get(url, max_bytes)

    # ---- 공통 필터
    @staticmethod
    def _accept(url: str, patterns: list[re.Pattern], site_dom: str, trusted: bool) -> Optional[str]:
        """기사 패턴에 맞으면 통과. 패턴이 없으면 휴리스틱: RSS 항목(trusted)은 숫자 4자리 이상,
        사이트맵·목록 페이지 링크는 5자리 이상(연도만 들어간 /main2022/ 같은 안내 페이지를 거르기 위해)."""
        nu = normalize_url(url)
        if not nu or registrable_domain(urlsplit(nu).netloc) != site_dom:
            return None
        if patterns and any(p.match(nu) for p in patterns):
            return nu
        if _EXCLUDE_RE.search(nu) or _NON_ARTICLE_PATH_RE.search(nu):
            return None
        if trusted and longest_digit_run(nu) >= 4:
            return nu
        if not patterns and longest_digit_run(nu) >= 5:
            return nu
        return None

    # ---- 1. RSS
    def from_rss(self, feeds: list[str], patterns: list[re.Pattern], site_dom: str, limit: int,
                 max_feeds: int = 3) -> list[dict]:
        out: list[dict] = []
        for feed in feeds[:max_feeds]:
            if len(out) >= limit:
                break
            res = self._get(feed, max_bytes=FEED_MAX_BYTES)
            if not res.ok or not res.content:
                continue
            fp = feedparser.parse(_gunzip_if_needed(res.content))
            for e in fp.entries:
                link = e.get("link")
                nu = self._accept(link, patterns, site_dom, trusted=True) if link else None
                if not nu:
                    continue
                out.append({"url": nu, "title": (e.get("title") or "").strip() or None,
                            "published": _struct_to_kst(e.get("published_parsed") or e.get("updated_parsed")),
                            "source": "rss"})
                if len(out) >= limit:
                    break
        return out

    # ---- 2. 사이트맵
    def _parse_urlset(self, content: bytes, patterns, site_dom, limit) -> list[dict]:
        out: list[dict] = []
        for block in _URL_BLOCK_RE.findall(content):
            m = _LOC_RE.search(block)
            if not m:
                continue
            loc = htmlmod.unescape(m.group(1).decode("utf-8", "ignore"))   # <loc> 는 XML 이라 &amp; 로 적혀 있다
            nu = self._accept(loc, patterns, site_dom, trusted=False)
            if not nu:
                continue
            d = _DATE_RE.search(block)
            t = _TITLE_RE.search(block)
            out.append({"url": nu, "title": t.group(1).decode("utf-8", "ignore") if t else None,
                        "published": d.group(1).decode("utf-8", "ignore") if d else None, "source": "sitemap"})
            if len(out) >= limit:
                break
        return out

    def from_sitemaps(self, sitemaps: list[str], patterns: list[re.Pattern], site_dom: str, limit: int,
                      max_fetch: int = 4) -> list[dict]:
        out: list[dict] = []
        queue = list(sitemaps)
        fetched = 0
        seen: set[str] = set()
        while queue and fetched < max_fetch and len(out) < limit:
            sm = queue.pop(0)
            if sm in seen:
                continue
            seen.add(sm)
            fetched += 1
            res = self._get(sm, max_bytes=SITEMAP_MAX_BYTES)
            if not res.ok or not res.content:
                continue
            content = _gunzip_if_needed(res.content)
            head = content[:3000].lower()
            if b"<sitemapindex" in head:
                children = [htmlmod.unescape(c.decode("utf-8", "ignore")) for blk in _SITEMAP_CHILD_RE.findall(content)
                            for c in _LOC_RE.findall(blk)[:1]]
                news_first = [c for c in children if re.search(r"news", c, re.I)]
                recent = [c for c in children if c not in news_first][-2:]
                queue = news_first[:2] + recent + queue
            elif b"<urlset" in head:
                out.extend(self._parse_urlset(content, patterns, site_dom, limit - len(out)))
        return out

    # ---- 3. 목록 페이지 / 홈페이지
    def from_pages(self, pages: list[str], patterns: list[re.Pattern], site_dom: str, limit: int,
                   max_fetch: int = 5) -> list[dict]:
        out: list[dict] = []
        for page in pages[:max_fetch]:
            if len(out) >= limit:
                break
            res = self._get(page)
            if not res.ok:
                continue
            tree = _parse(res.text)
            for link in collect_links(tree, res.final_url):
                nu = self._accept(link, patterns, site_dom, trusted=False)
                if nu:
                    out.append({"url": nu, "title": None, "published": None, "source": "page"})
                    if len(out) >= limit:
                        break
        return out

    # ---- 전체
    def collect(self, site: dict, limit: int = 50, strategies: tuple[str, ...] = ("rss", "sitemap", "pages")) -> list[dict]:
        site_dom = site.get("domain") or registrable_domain(urlsplit(site.get("home", "")).netloc)
        patterns = [re.compile(p) for p in (site.get("article_patterns") or [])]
        seen: set[str] = set()
        out: list[dict] = []

        def add(items: list[dict]) -> None:
            for it in items:
                if it["url"] not in seen:
                    seen.add(it["url"])
                    out.append(it)

        for strategy in strategies:
            if len(out) >= limit or self.tripped:
                break
            need = limit - len(out)
            if strategy == "rss" and site.get("rss"):
                add(self.from_rss(site["rss"], patterns, site_dom, need))
            elif strategy == "sitemap" and (site.get("news_sitemaps") or site.get("sitemaps")):
                add(self.from_sitemaps((site.get("news_sitemaps") or []) + (site.get("sitemaps") or []),
                                       patterns, site_dom, need))
            elif strategy == "pages":
                pages = [site["home"]] + (site.get("list_pages") or []) if site.get("home") else (site.get("list_pages") or [])
                add(self.from_pages(pages, patterns, site_dom, need))
        return out[:limit]


def collect_article_urls(site: dict, client=None, *, limit: int = 50, gap: float = 0.5,
                         strategies: tuple[str, ...] = ("rss", "sitemap", "pages")) -> list[dict]:
    c = Collector(client=client, gap=gap)
    try:
        return c.collect(site, limit=limit, strategies=strategies)
    except Exception as exc:  # 한 사이트의 목록 수집 실패가 전체를 멈추면 안 된다
        log.warning("collect failed for %s: %s", site.get("domain"), exc)
        return []
    finally:
        c.close()
