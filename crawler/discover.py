"""사이트 자동 탐지 모듈.

도메인(또는 홈페이지 URL) 하나를 받아, 사람이 설정 파일을 손으로 쓰지 않아도 되도록 아래를 자동으로 찾는다.
  1. robots.txt      : Sitemap 선언, Crawl-delay
  2. RSS 피드        : <link rel=alternate>, 페이지 안의 rss/feed 링크, 한국 CMS에 흔한 경로 추측(ndsoft 등),
                       RSS 안내 페이지(HTML)에 나열된 피드 재수집. 전부 실제로 받아서 항목이 있는지 검증
  3. 사이트맵        : robots.txt 선언 + 경로 추측. 뉴스 사이트맵(news 네임스페이스)은 따로 표시
  4. 기사 URL 패턴   : RSS 항목 링크(있으면) 또는 홈페이지 링크를 '템플릿'으로 일반화(숫자 ID → \\d+, 카테고리 자리 → [^/]+)해
                       가장 흔한 꼴을 정규식으로 만들고, 샘플 1건을 실제로 추출해 기사가 맞는지 검증
  5. 목록 페이지 후보: 홈페이지의 섹션 링크(기사 패턴이 아닌 얕은 경로)
결과는 SiteConfig 하나 = sites.yaml 의 블록 하나.
"""
from __future__ import annotations

import html as htmlmod
import logging
import re
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Optional
from urllib.parse import parse_qsl, quote_plus, urlencode, urljoin, urlsplit, urlunsplit

import feedparser

from .extract import _jsonld_nodes, _meta_map, _parse, _title_tag, extract_from_html, looks_like_html
from .fetch import FetchResult, fetch, make_client

log = logging.getLogger(__name__)

RSS_GUESSES = (
    "/rss/allArticle.xml", "/rss/S1N1.xml", "/rss/rss.xml", "/rss.xml", "/rss", "/feed",
    "/rss/news.xml", "/rss/all.xml", "/rss/index.xml", "/rss/headline.xml", "/rss/clickTop.xml",
    "/rss/total.xml", "/rss/news_all.xml", "/news/rss",
)
SITEMAP_GUESSES = (
    "/sitemap.xml", "/sitemap_index.xml", "/sitemap-news.xml", "/news-sitemap.xml", "/sitemap/news.xml",
    "/googlenews.xml", "/news_sitemap.xml", "/sitemap/sitemap.xml",
)
MAX_FEEDS = 12
MAX_SITEMAPS = 6
MAX_LIST_PAGES = 15
MAX_PATTERNS = 4
MAX_CANDIDATE_FETCHES = 24     # 사이트 하나당 피드 후보 검증에 쓰는 최대 요청 수
MAX_SITEMAP_FETCHES = 12       # 사이트 하나당 사이트맵 후보 확인에 쓰는 최대 요청 수
MAX_SITEMAP_LINKS = 300        # 기사 패턴 추론용으로 사이트맵에서 모으는 기사 주소 수
REQUEST_GAP = 0.4              # 같은 사이트에 연속 요청할 때 최소 간격(초). 탐지는 사이트당 한 번이므로 느려도 된다
SITE_TIME_BUDGET = 90.0        # 사이트 하나 탐지에 쓰는 최대 시간(초). 넘기면 남은 요청은 생략하고 찾은 것만 돌려준다
SITEMAP_MAX_BYTES = 2 * 1024 * 1024   # 사이트맵·피드는 앞부분만 있어도 충분

_EXCLUDE_RE = re.compile(
    r"(\.(?:css|js|jpe?g|png|gif|svg|webp|pdf|xml|ico|mp4|zip)(?:$|\?)|/rss(?:/|\.|$)|/feed(?:/|$)|/tags?/|/category/"
    r"|/search|/login|/member|/join|list|/page/\d+|sitemap|ad_view|mailto:|javascript:|/comment|/print)",
    re.I,
)
_LIST_JUNK_RE = re.compile(
    r"(subs|subscribe|notice|event|login|member|join|about|company|privacy|terms|contact|help|faq|policy|appdown"
    r"|store|sitemap|search|rss|mypage|advert|/ad/|/ads/|/pr/|/photo/?$|/tv/?$|/video/?$|popup|logout|/m/?$)", re.I)
_TRACKING_RE = re.compile(r"^(utm_|fbclid$|gclid$|igshid$)", re.I)
_SECOND_LEVEL = {"co", "or", "go", "ne", "re", "ac", "pe", "mil", "es", "kg", "ms", "hs", "sc",
                 "com", "net", "org", "gov", "edu"}
_FEED_HEAD_RE = re.compile(rb"<(rss|feed|rdf:rdf|\?xml)", re.I)
_SITEMAP_HEAD_RE = re.compile(rb"<(urlset|sitemapindex)", re.I)
_NEWS_NS_RE = re.compile(rb"sitemap-news|<news:", re.I)
_LOC_RE = re.compile(rb"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_META_REFRESH_RE = re.compile(r"""http-equiv=["']?refresh["']?[^>]*?url=([^"'>\s]+)""", re.I)
_JS_REDIRECT_RE = re.compile(
    r"""(?:location\.href|location\.replace\(|location\.assign\(|window\.location|document\.location)\s*=?\s*["']([^"']+)["']""",
    re.I)
_ID_TOKEN_RE = re.compile(r"\\d\+")
_NAME_SEPS = (" | ", " - ", " :: ", " – ", " — ", "|", ":")
_WILD_PATH = r"[^/]+"
_WILD_ID_PATH = r"[^/]*\d+[^/]*"
_WILD_QUERY = r"[^&]*"
_WILD_ID_QUERY = r"[^&]*\d+[^&]*"


@dataclass
class SiteConfig:
    name: str = ""
    domain: str = ""
    home: str = ""
    rss: list[str] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    news_sitemaps: list[str] = field(default_factory=list)
    article_patterns: list[str] = field(default_factory=list)
    list_pages: list[str] = field(default_factory=list)
    crawl_delay: Optional[float] = None
    encoding: str = ""
    discovered_at: str = ""
    verified_samples: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def has_source(self) -> bool:
        """수집 경로가 하나라도 있는가 (RSS, 뉴스 사이트맵, 검증된 기사 패턴)."""
        return bool(self.rss or self.news_sitemaps or self.article_patterns)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- URL 유틸

def registrable_domain(host: str) -> str:
    """news.site.co.kr → site.co.kr, www.site.com → site.com"""
    parts = host.lower().split(":")[0].split(".")
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


def normalize_url(url: str) -> str:
    """패턴 매칭·중복 제거용 정규화: 소문자 스킴/호스트, 추적 파라미터 제거, 파라미터 정렬, 프래그먼트 제거."""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    q = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING_RE.match(k))
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", urlencode(q), ""))


def longest_digit_run(s: str) -> int:
    return max((len(r) for r in re.findall(r"\d+", s)), default=0)


def url_tokens(norm_url: str) -> tuple[str, tuple[str, ...], tuple[tuple[str, str], ...], bool]:
    """정규화된 URL → (호스트, 경로 토큰들, 쿼리 (키, 값) 토큰들, 끝 슬래시 여부). 토큰은 정규식 조각.
    숫자 덩어리는 \\d+ 로 일반화: /arti/economy/it/1281224.html → ('arti','economy','it','\\d+\\.html')"""
    p = urlsplit(norm_url)
    segs = [s for s in p.path.split("/") if s]
    path: list[str] = []
    for s in segs:
        if re.fullmatch(r"\d+", s):
            path.append(r"\d+")
        elif re.search(r"\d{3,}", s):
            path.append(re.sub(r"\d{3,}", r"\\d+", re.escape(s)))
        else:
            path.append(re.escape(s))
    query: list[tuple[str, str]] = []
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        ek, ev = re.escape(quote_plus(k)), quote_plus(v)
        if re.fullmatch(r"\d+", v):
            query.append((ek, r"\d+"))
        elif re.search(r"\d{3,}", v):
            query.append((ek, re.sub(r"\d{3,}", r"\\d+", re.escape(ev))))
        else:
            query.append((ek, re.escape(ev)))
    host = p.netloc[4:] if p.netloc.startswith("www.") else p.netloc
    return host, tuple(path), tuple(query), bool(segs) and p.path.endswith("/")


def build_regex(host: str, path: tuple[str, ...], query: tuple[tuple[str, str], ...], trailing: bool) -> str:
    path_re = "/" + "/".join(path) + ("/" if trailing else "")
    q_re = (r"\?" + "&".join(f"{k}={v}" for k, v in query)) if query else ""
    return rf"^https?://(?:www\.)?{re.escape(host)}{path_re}{q_re}(?:[?#&].*)?$"


def url_template(norm_url: str) -> tuple[str, str]:
    """(호스트, 정규식 조각) — 단일 URL용 간편 함수."""
    host, path, query, trailing = url_tokens(norm_url)
    rx = build_regex(host, path, query, trailing)
    prefix = rf"^https?://(?:www\.)?{re.escape(host)}"
    return host, rx[len(prefix):-len(r"(?:[?#&].*)?$")]


def template_regex(host: str, key: str) -> str:
    return rf"^https?://(?:www\.)?{re.escape(host)}{key}(?:[?#&].*)?$"


def matches_any(url: str, patterns: list[str]) -> bool:
    nu = normalize_url(url)
    return bool(nu) and any(re.match(p, nu) for p in patterns)


def _merge_tokens(a_path, a_query, b_path, b_query):
    """두 템플릿이 '같은 꼴'이면 다른 자리를 와일드카드로 바꾼 병합 템플릿을, 아니면 None."""
    if len(a_path) != len(b_path) or len(a_query) != len(b_query):
        return None
    wild = (_WILD_PATH, _WILD_ID_PATH, _WILD_QUERY, _WILD_ID_QUERY)
    diffs = 0   # 이미 와일드카드인 자리는 세지 않는다 (병합이 거듭돼도 템플릿이 자연스럽게 넓어지도록)
    path: list[str] = []
    for x, y in zip(a_path, b_path):
        if x == y:
            path.append(x)
            continue
        xi, yi = bool(_ID_TOKEN_RE.search(x)), bool(_ID_TOKEN_RE.search(y))
        if xi != yi:
            return None
        path.append(_WILD_ID_PATH if xi else _WILD_PATH)
        diffs += 0 if x in wild else 1
    query: list[tuple[str, str]] = []
    for (xk, xv), (yk, yv) in zip(a_query, b_query):
        if xk != yk:
            return None
        if xv == yv:
            query.append((xk, xv))
            continue
        xi, yi = bool(_ID_TOKEN_RE.search(xv)), bool(_ID_TOKEN_RE.search(yv))
        if xi != yi:
            return None
        query.append((xk, _WILD_ID_QUERY if xi else _WILD_QUERY))
        diffs += 0 if xv in wild else 1
    if diffs > 2:
        return None
    # 전부 와일드카드 뿐인 템플릿(예: /[^/]+/\d+)은 너무 느슨하다. 글자 토큰이 하나 남거나, 쿼리가 있거나,
    # 숫자 토큰이 둘 이상(예: /[^/]+/[^/]+/\d+/\d+/\d+/[^/]+/ 같은 날짜 경로 구조)이어야 허용한다.
    literal = any(t not in (_WILD_PATH, _WILD_ID_PATH, r"\d+") for t in path)
    numeric = sum(1 for t in path if _ID_TOKEN_RE.search(t))
    if not (literal or query or numeric >= 2):
        return None
    return tuple(path), tuple(query)


def merge_groups(groups: dict) -> list[dict]:
    """groups: {(host, path, query, trailing): [urls]} → 병합된 템플릿 목록(많이 등장한 순)."""
    items = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    merged: list[dict] = []
    for (host, path, query, trailing), urls in items:
        for m in merged:
            if m["host"] != host or m["trailing"] != trailing:
                continue
            res = _merge_tokens(m["path"], m["query"], path, query)
            if res is None:
                continue
            m["path"], m["query"] = res
            m["urls"].extend(urls)
            break
        else:
            merged.append(dict(host=host, path=path, query=query, trailing=trailing, urls=list(urls)))
    merged.sort(key=lambda m: -len(m["urls"]))
    return merged


def collect_links(tree, base: str) -> list[str]:
    out: list[str] = []
    if tree is None:
        return out
    for href in tree.xpath("//a/@href"):
        h = (href or "").strip()
        if not h or h.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        try:
            u = urljoin(base, h)
        except ValueError:
            continue
        if u.startswith(("http://", "https://")):
            out.append(u)
    return out


def _dedupe(items):
    seen, out = set(), []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def clean_site_name(raw: Optional[str]) -> Optional[str]:
    """'미래를 보는 창 - 전자신문' → '전자신문', '연합뉴스 | 한국 대표 뉴스통신' → '연합뉴스'"""
    if not raw:
        return None
    t = re.sub(r"\s+", " ", raw).strip()
    for sep in _NAME_SEPS:
        if sep in t:
            segs = [s.strip() for s in t.split(sep) if s.strip()]
            if segs:
                t = min(segs, key=len)
            break
    return t[:40] or None


# ---------------------------------------------------------------- 탐지기

class Discoverer:
    def __init__(self, client=None, *, timeout: float = 12.0, verify: bool = True, gap: float = REQUEST_GAP):
        self.client = client or make_client(timeout)
        self._own_client = client is None
        self.timeout = timeout
        self.verify = verify
        self.gap = gap
        self._last = 0.0
        self._deadline = float("inf")
        self.requests = 0
        self.skipped = 0   # 시간 예산 초과로 생략한 요청 수
        self.blocked: Optional[str] = None   # 봇 차단 응답을 받았으면 이유. 그 뒤 요청은 보내지 않는다
        self.soft_403 = 0                    # 단순 403 연속 횟수 (추측 경로가 403인 건 흔하므로 3회 연속일 때만 차단으로 본다)
        # 마지막 discover() 의 중간 산출물 (LLM 보조 등 뒤 단계가 다시 요청하지 않고 쓰도록)
        self.home_tree = None
        self.page_links: list[str] = []
        self.declared_sitemaps: list[str] = []

    def close(self) -> None:
        if self._own_client:
            self.client.close()

    def _get(self, url: str, max_bytes: Optional[int] = None) -> FetchResult:
        if self.blocked:
            self.skipped += 1
            return FetchResult(url=url, final_url=url, error=f"blocked: {self.blocked}")
        if time.monotonic() > self._deadline:
            self.skipped += 1
            return FetchResult(url=url, final_url=url, error="site time budget exceeded")
        wait = self.gap - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.requests += 1
        kwargs = {"max_bytes": max_bytes} if max_bytes else {}
        res = fetch(url, client=self.client, timeout=self.timeout, retries=1, **kwargs)
        if res.blocked and (res.block_hard or self.soft_403 + 1 >= 3):
            self.blocked = res.blocked if res.block_hard else f"{res.blocked} ×{self.soft_403 + 1} 연속"
        elif res.blocked:
            self.soft_403 += 1
        elif res.status != 0:
            self.soft_403 = 0
        return res

    # ---- 1. 홈페이지
    def open_home(self, url_or_domain: str) -> FetchResult:
        """https ↔ http 를 서로 대체 시도하고, <meta refresh>/JS 리다이렉트 홈은 한 번 따라간다."""
        u = url_or_domain.strip()
        if "://" not in u:
            u = "https://" + u
        if urlsplit(u).path == "":
            u += "/"
        res = self._get(u)
        if not res.ok or not looks_like_html(res.text):
            alt_url = ("http://" + u[len("https://"):]) if u.startswith("https://") else ("https://" + u[len("http://"):])
            alt = self._get(alt_url)
            if alt.ok and looks_like_html(alt.text):
                res = alt
            elif alt.ok and not res.ok:
                res = alt
        if res.ok:
            head = res.text[:6000]
            m = (_META_REFRESH_RE.search(head) or _JS_REDIRECT_RE.search(head)) if len(res.text) < 20000 else None
            if m:
                target = urljoin(res.final_url, m.group(1).strip("'\" "))
                if registrable_domain(urlsplit(target).netloc) == registrable_domain(urlsplit(res.final_url).netloc):
                    hop = self._get(target)
                    if hop.ok and looks_like_html(hop.text):
                        return hop
        return res

    # ---- 2. robots.txt
    def read_robots(self, home: str) -> tuple[list[str], Optional[float]]:
        res = self._get(urljoin(home, "/robots.txt"))
        sitemaps: list[str] = []
        delay: Optional[float] = None
        if not res.ok or looks_like_html(res.text):
            return sitemaps, delay
        agent_all = False
        for line in res.text.splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip().lower(), v.strip()
            if k == "sitemap" and v:
                sitemaps.append(v)
            elif k == "user-agent":
                agent_all = v == "*"
            elif k == "crawl-delay" and agent_all:
                try:
                    delay = float(v)
                except ValueError:
                    pass
        return _dedupe(sitemaps), delay

    # ---- 3. RSS
    def _check_feed(self, url: str) -> tuple[str, list[str], str]:
        """(kind, entry_links, html) kind ∈ feed | html | bad"""
        res = self._get(url, max_bytes=SITEMAP_MAX_BYTES)
        if not res.ok or not res.content:
            return "bad", [], ""
        head = res.content[:4000].lstrip()
        if _FEED_HEAD_RE.search(head[:300]):
            fp = feedparser.parse(res.content)
            links = [e.get("link") for e in fp.entries if e.get("link")]
            return ("feed", links, "") if links else ("bad", [], "")
        if looks_like_html(res.text):
            return "html", [], res.text
        return "bad", [], ""

    def discover_feeds(self, home: str, tree, html_text: str) -> tuple[list[str], list[str], list[str]]:
        """(검증된 피드 URL들, 피드 항목 링크들, 메모)"""
        site_dom = registrable_domain(urlsplit(home).netloc)
        cands: list[tuple[str, str]] = []
        if tree is not None:
            for el in tree.xpath("//link[@href]"):
                t = (el.get("type") or "").lower()
                if "rss" in t or "atom" in t:
                    cands.append((urljoin(home, el.get("href").strip()), "link-tag"))
            for href in tree.xpath("//a/@href"):
                if re.search(r"(rss|feed|atom)", href or "", re.I):
                    u = urljoin(home, href.strip())
                    if registrable_domain(urlsplit(u).netloc) == site_dom:
                        cands.append((u, "href"))
        for g in RSS_GUESSES:
            cands.append((urljoin(home, g), "guess"))

        feeds: list[str] = []
        entry_links: list[str] = []
        notes: list[str] = []
        hows: Counter = Counter()
        tried: set[str] = set()
        fetches = 0
        queue = list(cands)
        while queue and fetches < MAX_CANDIDATE_FETCHES and len(feeds) < MAX_FEEDS:
            url, how = queue.pop(0)
            nu = normalize_url(url).rstrip("/")
            if not nu or nu in tried:
                continue
            tried.add(nu)
            # 추측 경로는 앞 단계에서 이미 '전체 기사' 피드를 찾았으면 생략
            if how == "guess" and feeds and any(re.search(r"all|total", f, re.I) for f in feeds):
                continue
            fetches += 1
            kind, links, page = self._check_feed(url)
            if kind == "feed":
                feeds.append(url)
                entry_links.extend(links)
                hows[how] += 1
            elif kind == "html" and re.search(r"rss|feed", url, re.I):
                # RSS 안내 페이지: 그 안의 피드 링크를 큐 앞에 넣어 검증
                sub = _parse(page)
                harvested = 0
                for h in (sub.xpath("//a/@href") if sub is not None else []):
                    if re.search(r"(\.xml|/rss|feed|atom)", h or "", re.I):
                        hu = urljoin(url, h.strip())
                        if registrable_domain(urlsplit(hu).netloc) == site_dom and normalize_url(hu).rstrip("/") not in tried:
                            queue.insert(harvested, (hu, "rss-page"))
                            harvested += 1
                            if harvested >= 15:
                                break
                if harvested:
                    notes.append(f"RSS 안내 페이지에서 피드 후보 {harvested}개 수집: {url}")
        feeds = _dedupe(feeds)
        feeds.sort(key=lambda f: 0 if re.search(r"all|total", f, re.I) else 1)   # 전체 기사 피드를 앞으로
        if hows:
            notes.append("RSS 발견 경로: " + ", ".join(f"{k}×{v}" for k, v in hows.items()))
        return feeds[:MAX_FEEDS], _dedupe(entry_links), notes

    # ---- 4. 사이트맵
    def discover_sitemaps(self, home: str, declared: list[str], want_links: bool = False
                          ) -> tuple[list[str], list[str], list[str]]:
        """(사이트맵들, 뉴스 사이트맵들, 기사 주소들). want_links 면 뉴스 사이트맵(없으면 일반 urlset)의
        <loc> 들을 기사 패턴 추론용으로 모은다(RSS가 없는 사이트의 대안)."""
        cands = _dedupe(list(declared) + [urljoin(home, g) for g in SITEMAP_GUESSES])
        found: list[str] = []
        news: list[str] = []
        news_links: list[str] = []
        plain_links: list[str] = []
        child_news: list[str] = []
        fetches = 0
        for c in cands:
            if len(found) >= MAX_SITEMAPS or fetches >= MAX_SITEMAP_FETCHES:
                break
            if c not in declared and len(found) >= 2:   # 추측은 두 개 찾으면 그만
                break
            fetches += 1
            res = self._get(c, max_bytes=SITEMAP_MAX_BYTES)
            if not res.ok or not res.content:
                continue
            head = res.content[:3000]
            if not _SITEMAP_HEAD_RE.search(head):
                continue
            found.append(c)
            is_news = bool(_NEWS_NS_RE.search(res.content[:20000]))
            if is_news:
                news.append(c)
            if b"<sitemapindex" in head.lower():
                for loc in _LOC_RE.findall(res.content)[:300]:
                    s = htmlmod.unescape(loc.decode("utf-8", "ignore"))
                    if re.search(r"news", s, re.I) and len(child_news) < 3:
                        child_news.append(s)
            else:   # <loc> 는 XML 이라 &amp; 로 적혀 있다 → 되돌린다
                locs = [htmlmod.unescape(l.decode("utf-8", "ignore")) for l in _LOC_RE.findall(res.content)[:MAX_SITEMAP_LINKS]]
                (news_links if is_news else plain_links).extend(locs)
        news.extend(child_news)
        if want_links and not news_links and child_news and fetches < MAX_SITEMAP_FETCHES:
            res = self._get(child_news[0], max_bytes=SITEMAP_MAX_BYTES)   # 인덱스가 가리킨 뉴스 사이트맵 하나만 더 읽는다
            if res.ok and res.content and b"<urlset" in res.content[:3000].lower():
                news_links.extend(htmlmod.unescape(l.decode("utf-8", "ignore")) for l in _LOC_RE.findall(res.content)[:MAX_SITEMAP_LINKS])
        links = news_links or plain_links if want_links else []
        return found, _dedupe(news), _dedupe(links)

    # ---- 5. 기사 URL 패턴
    def _group_templates(self, urls: list[str], site_dom: str, min_count: int) -> list[dict]:
        groups: dict = defaultdict(list)
        for u in urls:
            nu = normalize_url(u)
            if not nu or _EXCLUDE_RE.search(nu):
                continue
            if registrable_domain(urlsplit(nu).netloc) != site_dom:
                continue
            if longest_digit_run(nu) < 4:   # 기사 ID 없는 링크
                continue
            host, path, query, trailing = url_tokens(nu)
            if not any(_ID_TOKEN_RE.search(t) for t in path) and not any(_ID_TOKEN_RE.search(v) for _, v in query):
                continue
            groups[(host, path, query, trailing)].append(nu)
        merged = merge_groups(groups)
        return [m for m in merged if len(m["urls"]) >= min_count]

    def _verify_article(self, url: str) -> bool:
        """샘플 URL이 정말 '기사'인지: 추출이 되고, 긴 문단이 있고, 기사 메타 신호(og:type/JSON-LD/발행시각)나 날짜가 있어야 한다."""
        res = self._get(url)
        if not res.ok or not looks_like_html(res.text):
            return False
        art = extract_from_html(res.text, url, final_url=res.final_url)
        if not art.ok or not art.text:
            return False
        long_lines = sum(1 for line in art.text.splitlines() if len(line) >= 80)
        if long_lines == 0:   # 목록·섹션 페이지는 짧은 제목 줄만 있다
            return False
        tree = _parse(res.text)
        meta = _meta_map(tree)
        og_type = (meta.get("og:type") or "").lower()
        strong = ("article" in og_type or "news" in og_type or bool(meta.get("article:published_time"))
                  or any(str(n.get("@type", "")).lower().endswith("article") for n in _jsonld_nodes(tree)))
        # 기사 메타 신호가 없으면 날짜와 함께 '긴 문단이 둘 이상' 있어야 한다 (섹션 페이지의 머리글 한 줄에 속지 않도록)
        return strong or (art.date is not None and art.text_length >= 300 and long_lines >= 2)

    def infer_article_patterns(self, home: str, rss_links: list[str], sitemap_links: list[str],
                               page_links: list[str]) -> tuple[list[str], list[str], list[str]]:
        """기사 주소 출처 우선순위: RSS 항목 → 뉴스 사이트맵 항목 → 홈페이지 링크."""
        site_dom = registrable_domain(urlsplit(home).netloc)
        notes: list[str] = []
        items: list[dict] = []
        source = ""
        for source, links, min_count in (("rss", rss_links, 2), ("sitemap", sitemap_links, 3), ("homepage", page_links, 3)):
            items = self._group_templates(links, site_dom, min_count)
            if items:
                break
        if not items:
            notes.append("기사 URL 패턴 후보 없음")
            return [], [], notes
        patterns: list[str] = []
        samples: list[str] = []
        unverified: list[tuple[str, str]] = []
        for m in items[:MAX_PATTERNS]:
            rx = build_regex(m["host"], m["path"], m["query"], m["trailing"])
            sample = m["urls"][0]
            if not self.verify or self._verify_article(sample):
                patterns.append(rx)
                samples.append(sample)
            else:
                unverified.append((rx, sample))
        if not patterns and unverified:
            rx, u = unverified[0]
            patterns.append(rx)
            samples.append(u)
            notes.append("기사 패턴 샘플 추출 검증 실패, 가장 흔한 패턴을 미검증 상태로 기록")
        notes.append(f"기사 패턴 출처: {source} (후보 {len(items)}개 중 {len(patterns)}개 채택)")
        return patterns, samples, notes

    # ---- 6. 목록 페이지 후보
    def infer_list_pages(self, home: str, page_links: list[str], patterns: list[str]) -> list[str]:
        home_host = urlsplit(home).netloc.lower()
        site_dom = registrable_domain(home_host)
        compiled = [re.compile(p) for p in patterns]
        counts: Counter = Counter()
        bare = lambda h: h[4:] if h.startswith("www.") else h  # noqa: E731
        for u in page_links:
            nu = normalize_url(u)
            if not nu or registrable_domain(urlsplit(nu).netloc) != site_dom:
                continue
            sp0 = urlsplit(nu)
            if sp0.path in ("", "/") and not sp0.query and bare(sp0.netloc) == bare(home_host):
                continue   # 홈 자신
            if _EXCLUDE_RE.search(nu) and "list" not in nu.lower():
                continue
            if _LIST_JUNK_RE.search(nu):
                continue
            if any(p.match(nu) for p in compiled) or longest_digit_run(nu) >= 4:
                continue
            sp = urlsplit(nu)
            segs = [s for s in sp.path.split("/") if s]
            if len(segs) > 2 or len(sp.query) > 60:
                continue
            counts[nu] += 1 + (2 if bare(sp.netloc) == bare(home_host) else 0)   # 같은 호스트 우선
        return [u for u, _ in counts.most_common(MAX_LIST_PAGES)]

    # ---- 전체
    def discover(self, url_or_domain: str, *, name: Optional[str] = None) -> SiteConfig:
        cfg = SiteConfig(discovered_at=date.today().isoformat())
        started = time.monotonic()
        self._deadline = started + SITE_TIME_BUDGET
        res = self.open_home(url_or_domain)
        if not res.ok or not looks_like_html(res.text) or self.blocked:
            cfg.home = res.final_url or url_or_domain
            cfg.domain = registrable_domain(urlsplit(cfg.home).netloc) if "://" in cfg.home else url_or_domain
            cfg.name = name or cfg.domain
            if self.blocked:
                cfg.error = f"봇 차단 응답 → 탐지 중단 ({self.blocked}). 이 사이트는 정적 요청으로 수집하지 않는다"
            else:
                cfg.error = res.error or f"홈페이지가 HTML이 아님 (HTTP {res.status}, {res.content_type[:30] or 'content-type 없음'}, {len(res.content)}바이트)"
            return cfg
        home = res.final_url if urlsplit(res.final_url).path else res.final_url + "/"
        cfg.home = home
        cfg.domain = registrable_domain(urlsplit(home).netloc)
        cfg.encoding = res.encoding
        tree = _parse(res.text)
        meta = _meta_map(tree)
        cfg.name = (name or clean_site_name(meta.get("og:site_name") or meta.get("application-name"))
                    or clean_site_name(_title_tag(tree)) or cfg.domain)

        declared, cfg.crawl_delay = self.read_robots(home)
        cfg.rss, entry_links, notes = self.discover_feeds(home, tree, res.text)
        cfg.notes.extend(notes)
        cfg.sitemaps, cfg.news_sitemaps, sitemap_links = self.discover_sitemaps(home, declared, want_links=not entry_links)
        page_links = collect_links(tree, home)
        if len(page_links) < 5:   # 자바스크립트 앱 홈페이지: <a> 가 거의 없으면 스크립트 속 JSON 에 적힌 주소라도 모은다
            from .embedded import embedded_links
            extra = embedded_links(res.text, cfg.domain)
            if extra:
                page_links = page_links + extra
                cfg.notes.append(f"홈페이지 <a> 링크가 {len(page_links) - len(extra)}개뿐이라 스크립트 속 JSON 에서 주소 {len(extra)}개 보충")
        self.home_tree, self.page_links, self.declared_sitemaps = tree, page_links, declared
        cfg.article_patterns, cfg.verified_samples, notes = self.infer_article_patterns(
            home, entry_links, sitemap_links, page_links)
        cfg.notes.extend(notes)
        cfg.list_pages = self.infer_list_pages(home, page_links, cfg.article_patterns)
        if self.blocked:
            cfg.notes.append(f"탐지 도중 봇 차단 응답({self.blocked}) → 이후 요청 {self.skipped}회 생략. 찾은 것만 기록")
            if not cfg.has_source:
                cfg.error = f"봇 차단 응답 → 탐지 중단 ({self.blocked})"
        elif self.skipped:
            cfg.notes.append(f"시간 예산 {SITE_TIME_BUDGET:.0f}초 초과로 요청 {self.skipped}회 생략")
        cfg.notes.append(f"요청 {self.requests}회, {time.monotonic() - started:.1f}초")
        return cfg


def discover_site(url_or_domain: str, *, name: Optional[str] = None, client=None,
                  verify: bool = True, timeout: float = 12.0) -> SiteConfig:
    """도메인 하나 → SiteConfig. 예외를 밖으로 던지지 않는다."""
    d = Discoverer(client=client, timeout=timeout, verify=verify)
    try:
        return d.discover(url_or_domain, name=name)
    except Exception as exc:  # 탐지 실패가 전체 작업을 멈추면 안 된다
        log.warning("discover failed for %s: %s", url_or_domain, exc)
        cfg = SiteConfig(name=name or url_or_domain, home=url_or_domain, discovered_at=date.today().isoformat())
        cfg.error = f"{type(exc).__name__}: {exc}"[:200]
        return cfg
    finally:
        d.close()
