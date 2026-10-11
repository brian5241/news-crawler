"""기사 본문·메타데이터 추출 모듈.

추출 순서(앞 단계 본문이 FALLBACK_MIN_LEN 미만이면 다음 단계도 시도):
  1. trafilatura (기본, 정밀도 우선)
  2. JSON-LD (<script type="application/ld+json"> 의 NewsArticle.articleBody)
  3. trafilatura favor_recall=True (재현율 우선)
  4. readability-lxml (텍스트 밀도 기반, 설치돼 있을 때만)
  5. newspaper4k (설치돼 있을 때만)
어느 단계도 기준을 못 넘으면 가장 긴 결과를 쓰고, 그것도 MIN_TEXT_LEN 미만이면 ok=False.

메타데이터(제목·날짜·기자·언론사)는 어느 단계에서 뽑혔든 비어 있으면
<meta> 태그 → JSON-LD → <title> → 본문 바이라인 순으로 보완한다.
"""
from __future__ import annotations

import html as htmlmod
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

import trafilatura
from lxml import html as lxml_html

from .fetch import decode_html, fetch, make_client

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
MIN_TEXT_LEN = 80          # 최종 성공 기준(문자). 포토 기사처럼 짧은 정상 기사를 버리지 않기 위해 낮게 잡음
FALLBACK_MIN_LEN = 200     # 이 길이 미만이면 '조각만 뽑혔을 수 있다'고 보고 다음 추출기도 시도
MAX_TITLE_LEN = 300

_HTML_PARSER = lxml_html.HTMLParser(encoding="utf-8", remove_comments=True)
_HANGUL_RE = re.compile(r"[가-힣]")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_XML_DECL_RE = re.compile(r"^\s*<\?xml[^>]*\?>")
_HTML_ROOT_RE = re.compile(r"<(?:html|head|body)\b", re.I)
_HTML_BLOCK_RE = re.compile(r"<(?:div|p|article|main|section)\b", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_END_RE = re.compile(r"<br\s*/?>|</(?:p|div|li|h[1-6]|tr|section|article|blockquote)>", re.I)
_TITLES = "기자|특파원|논설위원|선임기자|전문기자|객원기자|인턴기자|편집위원|통신원|칼럼니스트"
_NAMES = r"(?:[가-힣]{2,5}\s*[·,]\s*)*[가-힣]{2,5}"   # '홍길동' 또는 '홍길동·김철수'
_BYLINE_RE = re.compile(rf"({_NAMES})\s*({_TITLES})")
_TRAILING_BYLINE_RE = re.compile(
    rf"^[\[(]?\s*(?:[가-힣A-Za-z0-9 ]+\s*[=|:]\s*)?{_NAMES}\s*(?:{_TITLES})\s*(?:[\w.+-]+@[\w.-]+)?\s*[\])]?$"
)
_BOILERPLATE_RE = re.compile(
    r"(무단\s*전재|재배포\s*금지|저작권자\s*[ⓒ©(]|^[ⓒ©]|Copyright\s*[ⓒ©(]?\s*\d|All\s+rights\s+reserved"
    r"|기사\s*제보|^[▶☞►]|^\[광고\]|뉴스레터\s*구독"
    r"|^(?:관련\s*기사|함께\s*보면\s*좋은\s*기사|많이\s*본\s*뉴스|인기\s*기사)$"
    # 본문이 비었을 때 공유 버튼 라벨이 본문으로 잡히는 것(ndsoft CMS 등)
    r"|기사\s*보내기|기사\s*저장|본문\s*글씨|이\s*기사를\s*공유합니다|URL\s*복사|다른\s*공유\s*찾기|^닫기$"
    # 구독·후원 안내 상자
    r"|구독하세요|정기\s*구독|후원자는\s*로그인|종이책\s*배송|광고\s*없이\s*기사를|^월\s*[\d,]+원)",
    re.I,
)
_DOMAIN_JUNK_RE = re.compile(r"\b(www|com|co|kr|net|org|html?)\b", re.I)
_TIME_RE = re.compile(r"\d{1,2}:\d{2}")
_ISO_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?)?"
)
_KO_DATE_RE = re.compile(
    r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?\.?(?:\s*\(?[월화수목금토일]\)?)?"
    r"(?:\s*(오전|오후)?\s*(\d{1,2})\s*[:시]\s*(\d{1,2})분?)?"
)
_TEXT_DATE_RE = re.compile(
    r"(?:기사입력|입력|등록|승인|게재|발행|작성)\s*[:：]?\s*(20\d{2}\s*[.\-/년]\s*\d{1,2}\s*[.\-/월]\s*\d{1,2}[^\n]{0,20})"
)
_DATE_META_KEYS = (
    "article:published_time", "article:published", "og:article:published_time", "pubdate", "publishdate",
    "publish_date", "published_time", "datepublished", "date", "dc.date", "dc.date.issued",
    "dcterms.created", "dcterms.issued", "og:regdate", "sailthru.date", "article.published",
)
_AUTHOR_META_KEYS = ("article:author", "author", "dable:author", "dc.creator", "byl", "sailthru.author")
_TITLE_SEPS = (" - ", " | ", " :: ", " – ", " — ", " › ", " » ")
_JSONLD_ARTICLE_TYPES = {
    "newsarticle", "article", "reportagenewsarticle", "analysisnewsarticle", "backgroundnewsarticle",
    "opinionnewsarticle", "reviewnewsarticle", "blogposting", "techarticle",
}


@dataclass
class Article:
    url: str
    final_url: str = ""
    title: Optional[str] = None
    text: Optional[str] = None
    date: Optional[str] = None          # "YYYY-MM-DD" 또는 "YYYY-MM-DD HH:MM" (KST)
    author: Optional[str] = None
    publisher: Optional[str] = None
    description: Optional[str] = None
    language: Optional[str] = None
    extractor: str = "none"             # trafilatura | jsonld | trafilatura:recall | readability | newspaper4k | *:short | meta-only
    text_length: int = 0
    ok: bool = False                    # text_length >= MIN_TEXT_LEN
    http_status: int = 0
    encoding: str = ""
    error: Optional[str] = None
    fetched_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- 정규화 유틸

def normalize_date(value: Any) -> Optional[str]:
    """여러 표기(ISO 8601, 2026.10.07 14:32, 2026년 10월 7일 오후 2시 32분 …)를
    'YYYY-MM-DD' 또는 'YYYY-MM-DD HH:MM'(KST)로 통일한다. 못 읽으면 None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    this_year = datetime.now().year
    m = _ISO_RE.search(s)
    if m:
        y, mo, d, hh, mm, _ss, tz = m.groups()
        try:
            if hh is None:
                dt = datetime(int(y), int(mo), int(d))
                return dt.strftime("%Y-%m-%d") if 2000 <= dt.year <= this_year + 1 else None
            dt = datetime(int(y), int(mo), int(d), int(hh), int(mm))
            if tz == "Z":
                dt = dt.replace(tzinfo=timezone.utc).astimezone(KST)
            elif tz:
                sign = 1 if tz[0] == "+" else -1
                digits = tz[1:].replace(":", "")
                off = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4])) * sign
                dt = dt.replace(tzinfo=timezone(off)).astimezone(KST)
            return dt.strftime("%Y-%m-%d %H:%M") if 2000 <= dt.year <= this_year + 1 else None
        except ValueError:
            return None
    m = _KO_DATE_RE.search(s)
    if not m:
        return None
    y, mo, d, ampm, hh, mm = m.groups()
    try:
        if hh is None:
            dt = datetime(int(y), int(mo), int(d))
            return dt.strftime("%Y-%m-%d") if 2000 <= dt.year <= this_year + 1 else None
        h = int(hh)
        if ampm == "오후" and h < 12:
            h += 12
        if ampm == "오전" and h == 12:
            h = 0
        dt = datetime(int(y), int(mo), int(d), h, int(mm))
        return dt.strftime("%Y-%m-%d %H:%M") if 2000 <= dt.year <= this_year + 1 else None
    except ValueError:
        return None


def clean_text(text: Optional[str]) -> str:
    """저작권 문구·링크 안내·끝부분 바이라인 같은 본문 아닌 줄을 걷어낸다."""
    if not text:
        return ""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _BOILERPLATE_RE.search(line):
            continue
        if _EMAIL_RE.fullmatch(line):
            continue
        out.append(line)
    while out and _TRAILING_BYLINE_RE.match(out[-1]):
        out.pop()
    return "\n".join(out)


_SENT_END_RE = re.compile(r"(?:다|요|죠|음|함|됨|임|了|。|\.|!|\?|”|\")\s*$")
_ELLIPSIS_RE = re.compile(r"(?:\.{2,}|…+|‥+)\s*$")   # 제목 목록의 말줄임(…)은 문장 끝맺음이 아니다


def looks_like_headline_list(text: str) -> bool:
    """본문이 아니라 '많이 본 기사' 같은 제목 목록을 잘못 집은 결과인지: 줄이 5개 이상인데 대부분 짧고 문장 끝맺음이 없다."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 5:
        return False
    lens = sorted(len(ln) for ln in lines)
    median = lens[len(lens) // 2]
    enders = sum(1 for ln in lines if _SENT_END_RE.search(_ELLIPSIS_RE.sub("", ln)))
    return median < 60 and enders / len(lines) < 0.3


def html_to_text(fragment: str) -> str:
    """HTML 조각 → 줄 단위 텍스트 (태그 제거, 엔티티 복원)."""
    s = _BLOCK_END_RE.sub("\n", fragment)
    s = _TAG_RE.sub(" ", s)
    s = htmlmod.unescape(s)
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def clean_author(value: Any) -> Optional[str]:
    if not value:
        return None
    s = _EMAIL_RE.sub("", str(value))
    s = re.sub(r"^\s*[\[(][^\])]*[\])]\s*", "", s)  # '[서울=뉴시스]' 같은 앞머리 제거
    s = re.sub(r"\s+", " ", s).strip(" \t[]()=:|,/·")
    if not s or s.lower().startswith("http") or len(s) > 60:
        return None
    bylines = _BYLINE_RE.findall(s)
    if bylines:  # '권원근 기자 Kwon Co Kr' → '권원근 기자' (이메일 조각 제거)
        names: list[str] = []
        for name, title in bylines:
            v = f"{name} {title}"
            if v not in names:
                names.append(v)
        return ", ".join(names)
    if _HANGUL_RE.search(s):
        # '남영주; Ichannela Com' → 한글 없는 도메인 조각 부분만 버린다
        parts = [p.strip() for p in re.split(r"\s*[;,/|]\s*", s) if p.strip()]
        keep = [p for p in parts if _HANGUL_RE.search(p) or not _DOMAIN_JUNK_RE.search(p)]
        return ", ".join(keep) if keep else None
    if _DOMAIN_JUNK_RE.search(s):  # 'Www Etnews Com' 같은 도메인 조각
        return None
    return s if re.search(r"[A-Za-z]", s) else None


def author_from_text(text: Optional[str]) -> Optional[str]:
    """본문 끝(우선)·앞의 '홍길동 기자' 꼴 바이라인."""
    if not text:
        return None
    tail = list(_BYLINE_RE.finditer(text[-600:]))
    if tail:
        m = tail[-1]
        return f"{m.group(1)} {m.group(2)}"
    head = _BYLINE_RE.search(text[:400])
    return f"{head.group(1)} {head.group(2)}" if head else None


def clean_title(title: Optional[str], strip_suffix: bool = False) -> Optional[str]:
    if not title:
        return None
    t = re.sub(r"\s+", " ", str(title)).strip()
    if strip_suffix:  # <title>의 ' - 언론사명' 같은 꼬리 제거
        for sep in _TITLE_SEPS:
            if sep in t:
                head, _, tail = t.rpartition(sep)
                if len(head) >= 8 and len(tail) <= 30:
                    t = head.strip()
                break
    return t[:MAX_TITLE_LEN] or None


def looks_like_html(text: str) -> bool:
    """<html>/<head>/<body> 가 있거나 블록 태그가 3개 이상이면 HTML로 본다."""
    head = text[:50000]
    return bool(_HTML_ROOT_RE.search(head)) or len(_HTML_BLOCK_RE.findall(head)) >= 3


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _now() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


# ---------------------------------------------------------------- HTML 보조 추출

def _parse(html_text: str):
    try:
        return lxml_html.fromstring(html_text.encode("utf-8"), parser=_HTML_PARSER)
    except Exception as exc:  # 깨진 HTML도 본문 추출기는 따로 시도하므로 여기선 포기만 한다
        log.debug("lxml parse failed: %s", exc)
        return None


def _meta_map(tree) -> dict[str, str]:
    out: dict[str, str] = {}
    if tree is None:
        return out
    for el in tree.iter("meta"):
        key = (el.get("property") or el.get("name") or el.get("itemprop") or "").strip().lower()
        val = (el.get("content") or "").strip()
        if key and val and key not in out:
            out[key] = val
    return out


def _first(meta: dict[str, str], keys: Iterable[str]) -> Optional[str]:
    for k in keys:
        v = meta.get(k)
        if v:
            return v
    return None


def _title_tag(tree) -> Optional[str]:
    if tree is None:
        return None
    el = tree.find(".//title")
    return el.text_content() if el is not None else None


def _time_tag(tree) -> Optional[str]:
    if tree is None:
        return None
    for v in tree.xpath("//time/@datetime"):
        if v and v.strip():
            return v.strip()
    return None


def _date_from_text(tree) -> Optional[str]:
    if tree is None:
        return None
    try:
        m = _TEXT_DATE_RE.search(tree.text_content())
    except Exception:
        return None
    return m.group(1) if m else None


def _jsonld_nodes(tree) -> list[dict]:
    """페이지의 모든 JSON-LD 객체를 평탄하게 모은다(@graph, 중첩 포함)."""
    out: list[dict] = []
    if tree is None:
        return out
    for el in tree.xpath('//script[@type="application/ld+json"]'):
        raw = (el.text or "").strip()
        if not raw:
            continue
        data = None
        for candidate in (raw, re.sub(r"[\x00-\x1f]", " ", raw).rstrip("; \n")):
            try:
                data = json.loads(candidate, strict=False)
                break
            except (json.JSONDecodeError, ValueError):
                continue
        if data is None:
            continue
        stack: list[Any] = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                out.append(node)
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return out


def _ld_name(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        return value.get("name") or None
    if isinstance(value, list):
        names = [_ld_name(v) for v in value]
        names = [n for n in names if n]
        return ", ".join(names) if names else None
    if isinstance(value, str):
        return value or None
    return None


# ---------------------------------------------------------------- 추출기 어댑터
# 모든 어댑터는 (html_text, url, tree) 를 받아 dict(title,text,date,author,publisher,description,language) 또는 None

def _run_trafilatura(html_text: str, url: str, tree=None, favor_recall: bool = False) -> Optional[dict]:
    try:
        out = trafilatura.extract(
            html_text, url=url, output_format="json", with_metadata=True,
            include_comments=False, include_tables=False, favor_recall=favor_recall, deduplicate=True,
        )
    except Exception as exc:
        log.debug("trafilatura failed on %s: %s", url, exc)
        return None
    if not out:
        return None
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return None
    return {
        "title": d.get("title"), "text": d.get("text"), "date": d.get("date"), "author": d.get("author"),
        "publisher": d.get("sitename"), "description": d.get("excerpt") or d.get("description"),
        "language": d.get("language"),
    }


def _run_jsonld(html_text: str, url: str, tree=None) -> Optional[dict]:
    for node in _jsonld_nodes(tree):
        t = node.get("@type")
        types = [t] if isinstance(t, str) else (t if isinstance(t, list) else [])
        if not any(str(x).lower() in _JSONLD_ARTICLE_TYPES for x in types):
            continue
        body = node.get("articleBody")
        if isinstance(body, list):
            body = "\n".join(str(b) for b in body)
        body = html_to_text(body) if isinstance(body, str) else ""
        return {
            "title": node.get("headline") or node.get("name"), "text": body,
            "date": node.get("datePublished") or node.get("dateCreated"),
            "author": _ld_name(node.get("author")), "publisher": _ld_name(node.get("publisher")),
            "description": node.get("description"), "language": node.get("inLanguage"),
        }
    return None


def _run_readability(html_text: str, url: str, tree=None) -> Optional[dict]:
    try:
        from readability import Document
    except ImportError:
        return None
    try:
        doc = Document(html_text, url=url)
        summary = doc.summary(html_partial=True)
        title = doc.short_title()
    except Exception as exc:
        log.debug("readability failed on %s: %s", url, exc)
        return None
    return {"title": title, "text": html_to_text(summary), "date": None, "author": None,
            "publisher": None, "description": None, "language": None}


def _run_newspaper(html_text: str, url: str, tree=None) -> Optional[dict]:
    try:
        from newspaper import Article as NPArticle
    except ImportError:
        return None
    try:
        try:
            art = NPArticle(url, language="ko", fetch_images=False)
        except TypeError:
            art = NPArticle(url, language="ko")
        art.download(input_html=html_text)
        art.parse()
    except Exception as exc:
        log.debug("newspaper4k failed on %s: %s", url, exc)
        return None
    pd = getattr(art, "publish_date", None)
    return {
        "title": art.title, "text": art.text, "date": pd.isoformat() if pd else None,
        "author": ", ".join(art.authors) if art.authors else None,
        "publisher": getattr(art, "meta_site_name", None) or None,
        "description": getattr(art, "meta_description", None) or None, "language": "ko",
    }


def _run_embedded(html_text: str, url: str, tree=None) -> Optional[dict]:
    """HTML 안의 JSON(Arc XP, Next.js, __INITIAL_STATE__ 등)에 숨은 본문. JS 로 화면을 그리는 사이트용."""
    try:
        from .embedded import article_from_embedded
        return article_from_embedded(html_text)
    except Exception as exc:
        log.debug("embedded json failed on %s: %s", url, exc)
        return None


_EXTRACTORS: tuple[tuple[str, Callable[..., Optional[dict]]], ...] = (
    ("trafilatura", _run_trafilatura),
    ("jsonld", _run_jsonld),
    ("embedded", _run_embedded),
    ("trafilatura:recall", lambda h, u, t: _run_trafilatura(h, u, t, favor_recall=True)),
    ("readability", _run_readability),
    ("newspaper4k", _run_newspaper),
)


# ---------------------------------------------------------------- 공개 API

def extract_from_html(content: bytes | str, url: str, *, final_url: Optional[str] = None,
                      http_status: int = 200, encoding: str = "") -> Article:
    """이미 받아둔 HTML에서 기사 추출. content 가 bytes 면 안전 디코딩을 거친다."""
    if isinstance(content, bytes):
        html_text, encoding = decode_html(content)
    else:
        html_text = content
    html_text = _XML_DECL_RE.sub("", html_text, count=1)
    art = Article(url=url, final_url=final_url or url, http_status=http_status, encoding=encoding, fetched_at=_now())
    if not looks_like_html(html_text):  # CSS·JS·이미지·JSON 등을 본문으로 오인하지 않도록
        art.error = "non-HTML content"
        return art

    tree = _parse(html_text)
    meta = _meta_map(tree)

    chosen: Optional[dict] = None
    partial: Optional[dict] = None
    partial_name = "none"
    listy: Optional[dict] = None          # 제목 목록처럼 보이는 결과는 최후의 수단으로만 쓴다
    listy_name = "none"
    for name, runner in _EXTRACTORS:
        r = runner(html_text, url, tree)
        if not r:
            continue
        text = clean_text(r.get("text"))
        r["text"] = text
        if text and looks_like_headline_list(text):
            if listy is None or len(text) > len(listy.get("text") or ""):
                listy, listy_name = r, name
            continue
        if len(text) >= FALLBACK_MIN_LEN:
            chosen, art.extractor = r, name
            break
        if partial is None or len(text) > len(partial.get("text") or ""):
            partial, partial_name = r, name
    best = chosen or partial or {}
    text = best.get("text") or ""
    if chosen is None:
        if not text:
            # 본문다운 결과가 하나도 없을 때: 메타 요약(og:description)이 있으면 그것을, 없으면 목록 결과라도 쓴다
            desc = clean_title(meta.get("og:description") or meta.get("description"))
            if desc and len(desc) >= MIN_TEXT_LEN:
                best, text, partial_name = dict(best, description=desc), desc, "meta-description"
            elif listy is not None:
                best, text, partial_name = listy, listy.get("text") or "", f"{listy_name}:list"
        art.extractor = f"{partial_name}:short" if len(text) >= MIN_TEXT_LEN else "meta-only"

    art.text = text or None
    art.text_length = len(text)
    art.ok = art.text_length >= MIN_TEXT_LEN

    ld = _run_jsonld(html_text, url, tree) or {}   # 메타데이터 보완용 (본문 추출기로 이미 썼어도 무방)
    art.title = (clean_title(best.get("title")) or clean_title(ld.get("title")) or clean_title(meta.get("og:title"))
                 or clean_title(_title_tag(tree), strip_suffix=True))
    art.publisher = (best.get("publisher") or ld.get("publisher") or meta.get("og:site_name")
                     or meta.get("application-name") or _domain(art.final_url))
    art.description = (meta.get("og:description") or meta.get("description") or ld.get("description")
                       or best.get("description") or None)
    art.date = (
        normalize_date(_first(meta, _DATE_META_KEYS))
        or normalize_date(ld.get("date"))
        or normalize_date(best.get("date"))
        or normalize_date(_date_from_text(tree))
        or normalize_date(_time_tag(tree))
    )
    art.author = (clean_author(best.get("author")) or clean_author(ld.get("author"))
                  or clean_author(_first(meta, _AUTHOR_META_KEYS)) or author_from_text(text))
    if _HANGUL_RE.search((art.title or "") + text[:500]):
        art.language = "ko"
    else:
        lang = best.get("language") or ld.get("language") or None
        if isinstance(lang, (list, tuple)):   # JSON-LD inLanguage 는 목록일 수 있다
            lang = next((str(x) for x in lang if x), None)
        art.language = str(lang)[:16] if lang else None
    for field_name in ("title", "author", "publisher", "description"):   # 어떤 출처든 문자열이 아니면 문자열로
        v = getattr(art, field_name)
        if v is not None and not isinstance(v, str):
            setattr(art, field_name, ", ".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v))
    return art


def extract_article(url: str, client=None, *, timeout: float = 15.0) -> Article:
    """URL 하나 → 기사. 네트워크 오류·HTTP 오류는 Article.error 에 담긴다."""
    res = fetch(url, client=client, timeout=timeout)
    if not res.ok:
        return Article(url=url, final_url=res.final_url, http_status=res.status,
                       error=res.error or f"HTTP {res.status}", encoding=res.encoding, fetched_at=_now())
    ctype = (res.content_type or "").lower()
    if ctype and "html" not in ctype and "xml" not in ctype and not looks_like_html(res.text):
        return Article(url=url, final_url=res.final_url, http_status=res.status, encoding=res.encoding,
                       error=f"non-HTML content: {ctype[:40]}", fetched_at=_now())
    return extract_from_html(res.text, url, final_url=res.final_url, http_status=res.status, encoding=res.encoding)


def extract_many(urls: Iterable[str], workers: int = 8, timeout: float = 15.0) -> list[Article]:
    """여러 URL을 스레드로 동시 처리. 입력 순서를 유지해 돌려준다."""
    urls = list(urls)
    with make_client(timeout) as client, ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda u: extract_article(u, client=client, timeout=timeout), urls))
