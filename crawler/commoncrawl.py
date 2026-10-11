"""Common Crawl 에서 특정 도메인의 기사 본문을 가져오는 모듈.

흐름:
  1. CC-INDEX API 로 도메인에 해당하는 크롤 레코드(offset/length/filename) 검색
  2. 각 레코드의 WARC 파일에서 Range 요청으로 해당 부분만 다운로드
  3. WARC 레코드에서 HTML 추출 → trafilatura 로 본문·메타 추출
"""
from __future__ import annotations

import gzip
import json
import logging
import re
import ssl
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Optional
from urllib.parse import urlsplit

import httpx
import trafilatura

from .fetch import DEFAULT_HEADERS, decode_html


def _make_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    return ctx


def _cc_client(timeout: float = 30) -> httpx.Client:
    return httpx.Client(headers=DEFAULT_HEADERS, timeout=timeout, verify=_make_ssl_context())

log = logging.getLogger(__name__)

CC_INDEX_BASE = "https://index.commoncrawl.org"
CC_DATA_BASE = "https://data.commoncrawl.org"
DEFAULT_INDEX = "CC-MAIN-2026-39"
REQUEST_GAP = 0.3


@dataclass
class CCRecord:
    url: str
    timestamp: str
    filename: str
    offset: int
    length: int
    status: int
    mime: str


@dataclass
class CCArticle:
    url: str
    title: Optional[str]
    text: Optional[str]
    date: Optional[str]
    author: Optional[str]
    text_length: int
    ok: bool
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "date": self.date,
            "author": self.author,
            "text_length": self.text_length,
            "ok": self.ok,
            "error": self.error,
        }


def list_indices(client: Optional[httpx.Client] = None) -> list[str]:
    own = client is None
    if own:
        client = _cc_client(15)
    try:
        r = client.get(f"{CC_INDEX_BASE}/collinfo.json")
        r.raise_for_status()
        return [idx["cdx-api"].split("/")[-1].replace("-index", "") for idx in r.json()]
    finally:
        if own:
            client.close()


def search_index(
    domain: str,
    *,
    index: str = DEFAULT_INDEX,
    limit: int = 50,
    url_filter: Optional[str] = None,
    client: Optional[httpx.Client] = None,
    retries: int = 2,
) -> list[CCRecord]:
    own = client is None
    if own:
        client = _cc_client(120)
    try:
        params: dict = {
            "url": url_filter or f"{domain}/*",
            "output": "json",
            "limit": limit,
        }

        url = f"{CC_INDEX_BASE}/{index}-index"
        r = None
        for attempt in range(retries + 1):
            try:
                r = client.get(url, params=params)
                if r.status_code == 504 and attempt < retries:
                    log.warning("CC 인덱스 타임아웃 (시도 %d/%d), 재시도...", attempt + 1, retries + 1)
                    time.sleep(2 * (attempt + 1))
                    continue
                break
            except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                if attempt < retries:
                    log.warning("CC 인덱스 타임아웃: %s (시도 %d/%d)", e, attempt + 1, retries + 1)
                    time.sleep(2 * (attempt + 1))
                    continue
                raise

        if r is None:
            return []
        if r.status_code == 404:
            return []
        if r.status_code == 504:
            log.warning("CC 인덱스 서버 타임아웃 (504). 서버가 바쁘거나 도메인이 너무 큽니다. "
                         "더 구체적인 URL 패턴(--url-filter)을 사용해 보세요.")
            return []
        r.raise_for_status()

        records = []
        for line in r.text.strip().split("\n"):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            status = int(d.get("status", 0))
            if status != 200:
                continue
            mime = d.get("mime", d.get("mime-detected", ""))
            if "html" not in mime.lower():
                continue
            records.append(CCRecord(
                url=d["url"],
                timestamp=d.get("timestamp", ""),
                filename=d["filename"],
                offset=int(d["offset"]),
                length=int(d["length"]),
                status=status,
                mime=mime,
            ))
        return records
    finally:
        if own:
            client.close()


def fetch_warc_record(record: CCRecord, client: Optional[httpx.Client] = None) -> Optional[str]:
    own = client is None
    if own:
        client = _cc_client(30)
    try:
        start = record.offset
        end = record.offset + record.length - 1
        warc_url = f"{CC_DATA_BASE}/{record.filename}"
        r = client.get(warc_url, headers={"Range": f"bytes={start}-{end}"})
        if r.status_code not in (200, 206):
            log.warning("WARC 가져오기 실패: %s (HTTP %d)", record.url, r.status_code)
            return None

        raw = r.content
        try:
            raw = gzip.decompress(raw)
        except (gzip.BadGzipFile, OSError):
            pass

        content = raw.decode("utf-8", errors="replace")
        # WARC 레코드에서 HTTP 응답 본문(HTML) 분리
        parts = content.split("\r\n\r\n", 2)
        if len(parts) >= 3:
            return parts[2]
        elif len(parts) == 2:
            return parts[1]
        return content
    finally:
        if own:
            client.close()


_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_OG_TITLE_RE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', re.I)
_OG_DATE_RE = re.compile(r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)', re.I)
_OG_AUTHOR_RE = re.compile(r'<meta[^>]+name=["\']author["\'][^>]+content=["\']([^"\']+)', re.I)


def _extract_meta(html: str) -> dict:
    meta: dict = {}
    m = _OG_TITLE_RE.search(html[:5000])
    if m:
        meta["title"] = m.group(1).strip()
    elif (m := _TITLE_TAG_RE.search(html[:5000])):
        raw = m.group(1).strip()
        for sep in (" - The New York Times", " - NYT", " | "):
            if sep in raw:
                raw = raw.split(sep)[0].strip()
                break
        meta["title"] = raw
    m = _OG_DATE_RE.search(html[:5000])
    if m:
        meta["date"] = m.group(1).strip()[:10]
    m = _OG_AUTHOR_RE.search(html[:5000])
    if m:
        meta["author"] = m.group(1).strip()
    return meta


def extract_from_html(html: str, url: str) -> CCArticle:
    try:
        html_meta = _extract_meta(html)
        result = trafilatura.bare_extraction(
            html,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
            favor_recall=True,
            url=url,
            as_dict=True,
        )
        if not result:
            return CCArticle(
                url=url, title=html_meta.get("title"), text=None,
                date=html_meta.get("date"), author=html_meta.get("author"),
                text_length=0, ok=False, error="추출 결과 없음",
            )
        text = result.get("text") or result.get("raw_text") or ""
        title = result.get("title") or html_meta.get("title")
        date = result.get("date") or html_meta.get("date")
        author = result.get("author") or html_meta.get("author")

        if not text or len(text) < 80:
            return CCArticle(
                url=url, title=title, text=text, date=date, author=author,
                text_length=len(text) if text else 0, ok=False,
                error="본문이 너무 짧음",
            )
        return CCArticle(
            url=url, title=title, text=text, date=date, author=author,
            text_length=len(text), ok=True,
        )
    except Exception as e:
        return CCArticle(
            url=url, title=None, text=None, date=None, author=None,
            text_length=0, ok=False, error=str(e)[:200],
        )


def crawl_domain(
    domain: str,
    *,
    index: str = DEFAULT_INDEX,
    limit: int = 50,
    url_filter: Optional[str] = None,
    gap: float = REQUEST_GAP,
    article_pattern: Optional[str] = None,
) -> list[CCArticle]:
    client = _cc_client(30)
    try:
        print(f"[CC] 인덱스 검색: {domain} (인덱스: {index}, 최대 {limit}건)")
        records = search_index(domain, index=index, limit=limit,
                               url_filter=url_filter, client=client)
        print(f"[CC] 검색 결과: {len(records)}건")

        if article_pattern:
            pat = re.compile(article_pattern)
            records = [r for r in records if pat.search(r.url)]
            print(f"[CC] 패턴 필터 후: {len(records)}건")

        articles: list[CCArticle] = []
        for i, rec in enumerate(records, 1):
            print(f"[CC] [{i}/{len(records)}] {rec.url[:80]}...", flush=True)
            html = fetch_warc_record(rec, client=client)
            if not html:
                articles.append(CCArticle(
                    url=rec.url, title=None, text=None, date=None, author=None,
                    text_length=0, ok=False, error="WARC 레코드 가져오기 실패",
                ))
                continue
            art = extract_from_html(html, rec.url)
            articles.append(art)
            status = "OK" if art.ok else f"FAIL ({art.error})"
            print(f"       {status} | {art.title or '제목없음'} | {art.text_length}자")
            if gap > 0 and i < len(records):
                time.sleep(gap)

        ok_count = sum(1 for a in articles if a.ok)
        print(f"\n[CC] 완료: 전체 {len(articles)}건, 성공 {ok_count}건, "
              f"실패 {len(articles) - ok_count}건")
        return articles
    finally:
        client.close()
