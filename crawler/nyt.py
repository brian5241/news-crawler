"""New York Times Article Search API 연동.

developer.nytimes.com 에서 무료 키를 발급받아 사용한다.
본문 전체는 제공하지 않고, 제목·요약·첫 문단·URL 등 메타데이터를 돌려준다.
전문이 필요하면 URL 로 웹 추출을 시도할 수 있다.

환경변수 NYT_API_KEY 또는 --api-key 인자로 키를 전달한다.
제한: 분당 5건, 일 500건 (무료 티어).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
API_BASE = "https://api.nytimes.com/svc/search/v2"
TOP_STORIES_BASE = "https://api.nytimes.com/svc/topstories/v2"
RATE_GAP = 12.0  # 분당 5건 제한 → 12초 간격
PAGE_SIZE = 10    # NYT API 고정 (변경 불가)
MAX_PAGES = 100   # API 제한: page 0~99


def _load_dotenv():
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if value and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


@dataclass
class NYTArticle:
    url: str
    title: str
    abstract: str
    lead_paragraph: str
    date: Optional[str] = None
    author: Optional[str] = None
    section: Optional[str] = None
    source: Optional[str] = None
    word_count: int = 0
    text_length: int = 0
    ok: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "abstract": self.abstract,
            "lead_paragraph": self.lead_paragraph,
            "date": self.date,
            "author": self.author,
            "section": self.section,
            "source": self.source,
            "word_count": self.word_count,
            "text_length": self.text_length,
            "ok": self.ok,
        }


def _normalize_date(iso: str) -> Optional[str]:
    """'2026-10-07T14:32:00+0000' → '2026-10-07 23:32' (KST)"""
    if not iso:
        return None
    try:
        iso = iso.replace("+0000", "+00:00").replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        kst = dt.astimezone(KST)
        return kst.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso[:10] if len(iso) >= 10 else None


def _extract_byline(doc: dict) -> Optional[str]:
    byline = doc.get("byline") or {}
    original = byline.get("original") or ""
    if original.upper().startswith("BY "):
        original = original[3:]
    return original.strip() or None


def _parse_doc(doc: dict) -> NYTArticle:
    headline = doc.get("headline") or {}
    title = headline.get("main") or headline.get("print_headline") or ""
    abstract = doc.get("abstract") or doc.get("snippet") or ""
    lead = doc.get("lead_paragraph") or ""
    combined = f"{abstract}\n\n{lead}".strip() if lead and lead != abstract else abstract

    return NYTArticle(
        url=doc.get("web_url", ""),
        title=title,
        abstract=abstract,
        lead_paragraph=lead,
        date=_normalize_date(doc.get("pub_date", "")),
        author=_extract_byline(doc),
        section=doc.get("section_name"),
        source=doc.get("source"),
        word_count=int(doc.get("word_count") or 0),
        text_length=len(combined),
        ok=bool(title and (abstract or lead)),
    )


class NYTClient:
    def __init__(self, api_key: Optional[str] = None, gap: float = RATE_GAP):
        _load_dotenv()
        self.api_key = api_key or os.environ.get("NYT_API_KEY", "")
        self.gap = gap
        self._last_request = 0.0
        self._client = httpx.Client(timeout=30.0, follow_redirects=True)

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def _wait(self):
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.gap:
            time.sleep(self.gap - elapsed)
        self._last_request = time.monotonic()

    def _get(self, url: str, params: dict) -> dict:
        params["api-key"] = self.api_key
        self._wait()
        resp = self._client.get(url, params=params)
        if resp.status_code == 401:
            raise RuntimeError(
                "NYT API 인증 실패 (401). API 키를 확인하세요.\n"
                "  발급: https://developer.nytimes.com/accounts/create\n"
                "  사용: --api-key KEY  또는  set NYT_API_KEY=KEY"
            )
        if resp.status_code == 429:
            raise RuntimeError("NYT API 호출 한도 초과 (429). 분당 5건, 일 500건 제한. 잠시 후 다시 시도하세요.")
        resp.raise_for_status()
        return resp.json()

    def search(
        self,
        query: Optional[str] = None,
        *,
        fq: Optional[str] = None,
        begin_date: Optional[str] = None,
        end_date: Optional[str] = None,
        sort: str = "newest",
        page: int = 0,
    ) -> tuple[list[NYTArticle], int]:
        """기사 검색. (기사 목록, 전체 건수)
        fq: Lucene 필터. 예) 'section_name:("World")' 또는 'source:("The New York Times")'
        날짜 형식: YYYYMMDD
        """
        params: dict[str, Any] = {"sort": sort, "page": page}
        if query:
            params["q"] = query
        if fq:
            params["fq"] = fq
        if begin_date:
            params["begin_date"] = begin_date.replace("-", "")
        if end_date:
            params["end_date"] = end_date.replace("-", "")

        data = self._get(f"{API_BASE}/articlesearch.json", params)
        response = data.get("response", {})
        docs = response.get("docs", [])
        total = response.get("meta", {}).get("hits", 0)
        articles = [_parse_doc(d) for d in docs]
        return articles, total

    def search_all(
        self,
        query: Optional[str] = None,
        *,
        fq: Optional[str] = None,
        begin_date: Optional[str] = None,
        end_date: Optional[str] = None,
        sort: str = "newest",
        limit: int = 50,
    ) -> list[NYTArticle]:
        """여러 페이지에 걸쳐 최대 limit건까지 수집.
        분당 5건 제한 때문에 느림 (10건당 12초).
        """
        all_articles: list[NYTArticle] = []
        page = 0
        while len(all_articles) < limit and page < MAX_PAGES:
            articles, total = self.search(
                query, fq=fq, begin_date=begin_date, end_date=end_date,
                sort=sort, page=page,
            )
            if not articles:
                break
            all_articles.extend(articles)
            total_pages = min((total + PAGE_SIZE - 1) // PAGE_SIZE, MAX_PAGES)
            log.info("NYT page %d/%d: %d docs (total %d)", page + 1, total_pages, len(articles), total)
            if page + 1 >= total_pages:
                break
            page += 1
        return all_articles[:limit]

    def top_stories(self, section: str = "home") -> list[NYTArticle]:
        """Top Stories API. section: home, world, business, technology, science, health, sports, arts, …"""
        data = self._get(f"{TOP_STORIES_BASE}/{section}.json", {})
        results = data.get("results", [])
        articles = []
        for r in results:
            abstract = r.get("abstract") or ""
            articles.append(NYTArticle(
                url=r.get("url", ""),
                title=r.get("title", ""),
                abstract=abstract,
                lead_paragraph="",
                date=_normalize_date(r.get("published_date", "")),
                author=r.get("byline", "").replace("By ", "") if r.get("byline") else None,
                section=r.get("section"),
                source="The New York Times",
                text_length=len(abstract),
                ok=bool(r.get("title")),
            ))
        return articles
