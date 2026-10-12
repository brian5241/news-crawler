"""The Guardian Open Platform API 연동.

Content API (https://open-platform.theguardian.com/documentation/)를 통해
기사를 검색·수집한다. 웹 스크래핑 없이 공식 API로 본문까지 받을 수 있다.

환경변수 GUARDIAN_API_KEY 또는 --api-key 인자로 키를 전달한다.
키가 없으면 'test' 키(일 500건, 본문 없음)로 제한적 동작.
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
API_BASE = "https://content.guardianapis.com"
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
RATE_GAP = 0.25  # 초당 4건 정도


@dataclass
class GuardianArticle:
    url: str
    title: str
    text: str
    date: Optional[str] = None
    author: Optional[str] = None
    section: Optional[str] = None
    pillar: Optional[str] = None
    word_count: int = 0
    text_length: int = 0
    ok: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "date": self.date,
            "author": self.author,
            "section": self.section,
            "pillar": self.pillar,
            "word_count": self.word_count,
            "text_length": self.text_length,
            "ok": self.ok,
        }


def _normalize_date(iso: str) -> Optional[str]:
    """'2026-10-07T14:32:00Z' → '2026-10-07 23:32' (KST)"""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        kst = dt.astimezone(KST)
        return kst.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso[:10] if len(iso) >= 10 else None


def _extract_byline(tags: list[dict]) -> Optional[str]:
    contributors = [t.get("webTitle", "") for t in tags if t.get("type") == "contributor"]
    return ", ".join(contributors) if contributors else None


def _parse_article(item: dict) -> GuardianArticle:
    fields = item.get("fields") or {}
    body = fields.get("bodyText") or ""
    tags = item.get("tags") or []

    art = GuardianArticle(
        url=item.get("webUrl", ""),
        title=item.get("webTitle") or fields.get("headline") or "",
        text=body,
        date=_normalize_date(item.get("webPublicationDate", "")),
        author=fields.get("byline") or _extract_byline(tags),
        section=item.get("sectionName"),
        pillar=item.get("pillarName"),
        word_count=int(fields.get("wordcount") or 0),
        text_length=len(body),
        ok=len(body) >= 80,
    )
    return art


def _load_dotenv():
    """프로젝트 루트의 .env 파일에서 환경변수를 읽는다."""
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


class GuardianClient:
    def __init__(self, api_key: Optional[str] = None, gap: float = RATE_GAP):
        _load_dotenv()
        self.api_key = api_key or os.environ.get("GUARDIAN_API_KEY", "test")
        self.gap = gap
        self._last_request = 0.0
        self._client = httpx.Client(timeout=30.0, follow_redirects=True)

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _wait(self):
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.gap:
            time.sleep(self.gap - elapsed)
        self._last_request = time.monotonic()

    @property
    def has_key(self) -> bool:
        return bool(self.api_key) and self.api_key != "test"

    def _get(self, endpoint: str, params: dict) -> dict:
        params["api-key"] = self.api_key
        url = f"{API_BASE}/{endpoint}?{urlencode(params)}"
        self._wait()
        resp = self._client.get(url)
        if resp.status_code == 401:
            raise RuntimeError(
                "Guardian API 인증 실패 (401). API 키를 확인하세요.\n"
                "  발급: https://bonobo.capi.gutools.co.uk/register/developer\n"
                "  사용: --api-key KEY  또는  set GUARDIAN_API_KEY=KEY"
            )
        if resp.status_code == 429:
            raise RuntimeError("Guardian API 호출 한도 초과 (429). 잠시 후 다시 시도하세요.")
        resp.raise_for_status()
        data = resp.json()
        response = data.get("response", {})
        if response.get("status") != "ok":
            raise RuntimeError(f"Guardian API error: {response.get('message', data)}")
        return response

    def search(
        self,
        query: Optional[str] = None,
        *,
        section: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        order_by: str = "newest",
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        show_fields: str = "bodyText,headline,byline,wordcount",
        show_tags: str = "contributor",
    ) -> tuple[list[GuardianArticle], int, int]:
        """기사 검색. (기사 목록, 전체 건수, 전체 페이지 수)"""
        params: dict[str, Any] = {
            "order-by": order_by,
            "page": page,
            "page-size": min(page_size, MAX_PAGE_SIZE),
            "show-fields": show_fields,
            "show-tags": show_tags,
        }
        if query:
            params["q"] = query
        if section:
            params["section"] = section
        if from_date:
            params["from-date"] = from_date
        if to_date:
            params["to-date"] = to_date

        response = self._get("search", params)
        articles = [_parse_article(item) for item in response.get("results", [])]
        total = response.get("total", 0)
        pages = response.get("pages", 1)
        return articles, total, pages

    def search_all(
        self,
        query: Optional[str] = None,
        *,
        section: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        order_by: str = "newest",
        limit: int = 100,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> list[GuardianArticle]:
        """여러 페이지에 걸쳐 최대 limit건까지 수집."""
        all_articles: list[GuardianArticle] = []
        page = 1
        while len(all_articles) < limit:
            remaining = limit - len(all_articles)
            ps = min(page_size, remaining, MAX_PAGE_SIZE)
            articles, total, total_pages = self.search(
                query,
                section=section,
                from_date=from_date,
                to_date=to_date,
                order_by=order_by,
                page=page,
                page_size=ps,
            )
            if not articles:
                break
            all_articles.extend(articles)
            log.info("Guardian page %d/%d: %d articles (total %d)", page, total_pages, len(articles), total)
            if page >= total_pages:
                break
            page += 1
        return all_articles[:limit]

    def sections(self) -> list[dict[str, str]]:
        """사용 가능한 섹션 목록."""
        response = self._get("sections", {})
        return [{"id": s["id"], "name": s["webTitle"]} for s in response.get("results", [])]

    def get_article(self, article_id: str) -> Optional[GuardianArticle]:
        """개별 기사 조회. article_id 예: 'world/2026/oct/07/article-slug'"""
        params = {
            "show-fields": "bodyText,headline,byline,wordcount",
            "show-tags": "contributor",
        }
        try:
            response = self._get(article_id, params)
            content = response.get("content")
            if content:
                return _parse_article(content)
        except Exception as exc:
            log.warning("Guardian get_article failed: %s", exc)
        return None
