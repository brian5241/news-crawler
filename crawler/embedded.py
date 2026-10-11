"""HTML 안에 JSON 으로 숨어 있는 기사 본문·링크 꺼내기 (6단계의 가벼운 길).

자바스크립트로 화면을 그리는 사이트 중 상당수는 본문을 HTML 안의 JSON 덩어리에 실어 보낸다.
브라우저 없이도 그 JSON 만 읽으면 본문이 나온다. 지원하는 꼴:
  - Arc XP (조선일보 등): `Fusion.globalContent={...}` 의 content_elements
  - Next.js: `<script id="__NEXT_DATA__" type="application/json">`
  - `window.__INITIAL_STATE__=` / `__PRELOADED_STATE__=` / `__APOLLO_STATE__=` / `__NUXT__=` (JSON 으로 적힌 경우만)
  - 그 밖의 `<script type="application/json">`
JSON 구조를 모르는 경우엔 '문장처럼 보이는 긴 문자열'들을 같은 자리(경로) 끼리 묶어 가장 큰 묶음을 본문으로 본다.
본문을 XHR(별도 요청)로 받아오는 사이트는 여기서 못 한다 — 그건 Playwright 영역.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Optional
from urllib.parse import urlsplit

_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)
_JSON_TYPE_RE = re.compile(r"""type\s*=\s*["']application/(?:json|ld\+json)["']""", re.I)
_LD_TYPE_RE = re.compile(r"""type\s*=\s*["']application/ld\+json["']""", re.I)
_ASSIGN_RE = re.compile(
    r"(?:Fusion\.globalContent|window\.__INITIAL_STATE__|__INITIAL_STATE__|window\.__PRELOADED_STATE__|__PRELOADED_STATE__"
    r"|window\.__APOLLO_STATE__|__APOLLO_STATE__|window\.__NUXT__|window\.__data|window\.__STATE__|__STATE__)\s*=\s*(?=\{)")
_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+")
_SENT_END_RE = re.compile(r"(?:다|요|죠|음|함|됨|임|了|。|\.|!|\?|”|\")\s*$")
_TITLE_KEYS = ("headline", "title", "subject", "articleTitle", "news_title", "tit")
_DATE_KEYS = ("display_date", "datePublished", "publishedAt", "published_at", "publish_date", "pubDate", "first_publish_date",
              "created_date", "regDate", "reg_date", "date", "writeDate", "wdate")
_AUTHOR_KEYS = ("author", "byline", "writer", "reporter", "credits")
MIN_PARAGRAPH = 60
MIN_BODY = 200


def balanced_json(s: str, start: int) -> Optional[str]:
    """s[start] 가 '{' 또는 '[' 인 자리부터 괄호 균형이 맞는 조각을 잘라낸다 (문자열 속 괄호는 무시)."""
    if start < 0 or start >= len(s) or s[start] not in "{[":
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    return s[start:i + 1]
    return None


def _loads(text: str) -> Optional[Any]:
    try:
        return json.loads(text, strict=False)
    except (json.JSONDecodeError, ValueError):
        return None


def find_embedded_json(html_text: str) -> list[tuple[str, Any]]:
    """페이지 안의 JSON 덩어리들 → [(출처 이름, 파이썬 객체)]. JSON-LD 는 extract.py 가 따로 다루므로 뺀다."""
    out: list[tuple[str, Any]] = []
    for m in _SCRIPT_RE.finditer(html_text):
        attrs, body = m.group(1), m.group(2).strip()
        if not body:
            continue
        if _LD_TYPE_RE.search(attrs):
            continue
        if _JSON_TYPE_RE.search(attrs):
            obj = _loads(body)
            if obj is not None:
                label = "__NEXT_DATA__" if "__NEXT_DATA__" in attrs else "json-script"
                out.append((label, obj))
            continue
        for am in _ASSIGN_RE.finditer(body):
            frag = balanced_json(body, am.end())
            if frag:
                obj = _loads(frag)
                if obj is not None:
                    out.append((am.group(0).split("=")[0].strip().replace("window.", ""), obj))
    return out


def _strip(s: str) -> str:
    s = _TAG_RE.sub(" ", s)
    return re.sub(r"[ \t\xa0]+", " ", s).strip()


def _looks_like_prose(s: str) -> bool:
    if len(s) < MIN_PARAGRAPH or s.startswith(("http://", "https://", "//", "data:", "{", "[")):
        return False
    if s.count("<") > 20 and "</" in s and len(_strip(s)) < 0.3 * len(s):   # 거의 태그뿐인 HTML
        return False
    t = _strip(s)
    if len(t) < MIN_PARAGRAPH:
        return False
    return bool(_SENT_END_RE.search(t)) or len(re.findall(r"[가-힣]", t)) >= 20


def _walk(obj: Any, path: str = "$", out: Optional[list] = None, depth: int = 0) -> list:
    """(경로 모양, 값, 원래 순서) 목록. 리스트 인덱스는 [] 로 뭉갠다."""
    if out is None:
        out = []
    if depth > 40:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk(v, f"{path}.{k}", out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, f"{path}[]", out, depth + 1)
    elif isinstance(obj, str):
        out.append((path, obj, len(out)))
    return out


def _first_key(obj: Any, keys: tuple[str, ...], depth: int = 0) -> Optional[Any]:
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k]:
                return obj[k]
        for v in obj.values():
            r = _first_key(v, keys, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj[:20]:
            r = _first_key(v, keys, depth + 1)
            if r is not None:
                return r
    return None


def _name(v: Any) -> Optional[str]:
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        if "by" in v and isinstance(v["by"], list):       # Arc XP credits
            return ", ".join(n for n in (_name(x) for x in v["by"]) if n) or None
        return v.get("name") or v.get("basic") or None
    if isinstance(v, list):
        names = [n for n in (_name(x) for x in v) if n]
        return ", ".join(names) or None
    return None


def _arc_xp(obj: Any) -> Optional[dict]:
    if not isinstance(obj, dict) or not isinstance(obj.get("content_elements"), list):
        return None
    paras = [_strip(e.get("content", "")) for e in obj["content_elements"]
             if isinstance(e, dict) and e.get("type") == "text" and e.get("content")]
    paras = [p for p in paras if p]
    if sum(len(p) for p in paras) < MIN_BODY:
        return None
    head = obj.get("headlines") or {}
    return {
        "title": head.get("basic") if isinstance(head, dict) else None,
        "text": "\n".join(paras),
        "date": obj.get("display_date") or obj.get("first_publish_date") or obj.get("created_date"),
        "author": _name(obj.get("credits")),
        "publisher": None, "description": obj.get("description", {}).get("basic") if isinstance(obj.get("description"), dict) else None,
        "language": obj.get("language"),
    }


def _generic(obj: Any) -> Optional[dict]:
    """문장처럼 보이는 긴 문자열을 같은 경로끼리 묶어, 가장 큰 묶음을 본문으로."""
    groups: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for path, val, order in _walk(obj):
        if _looks_like_prose(val):
            groups[path].append((order, _strip(val)))
    if not groups:
        return None
    best_path, items = max(groups.items(), key=lambda kv: sum(len(t) for _, t in kv[1]))
    items.sort()
    seen: set[str] = set()
    paras = []
    for _, t in items:
        if t not in seen:
            seen.add(t)
            paras.append(t)
    text = "\n".join(paras)
    if len(text) < MIN_BODY:
        return None
    title = _first_key(obj, _TITLE_KEYS)
    date = _first_key(obj, _DATE_KEYS)
    author = _first_key(obj, _AUTHOR_KEYS)
    return {"title": _name(title) if not isinstance(title, str) else title, "text": text,
            "date": date if isinstance(date, (str, int, float)) else None, "author": _name(author),
            "publisher": None, "description": None, "language": None}


def article_from_embedded(html_text: str) -> Optional[dict]:
    """페이지 안 JSON 에서 기사(title, text, date, author, …)를 찾는다. 없으면 None."""
    blobs = find_embedded_json(html_text)
    for _label, obj in blobs:          # 구조를 아는 꼴 먼저
        r = _arc_xp(obj)
        if r:
            return r
    best: Optional[dict] = None
    for _label, obj in blobs:
        r = _generic(obj)
        if r and (best is None or len(r["text"]) > len(best["text"])):
            best = r
    return best


def embedded_links(html_text: str, site_domain: str) -> list[str]:
    """스크립트 속 JSON 에 적힌 같은 도메인 절대 URL 들 (자바스크립트 앱 홈페이지에서 링크를 모을 때)."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _SCRIPT_RE.finditer(html_text):
        for u in _URL_RE.findall(m.group(2)):
            u = u.rstrip(",;)")
            try:
                host = urlsplit(u).netloc.lower()
            except ValueError:
                continue
            if host.endswith(site_domain) and u not in seen and not re.search(r"\.(?:js|css|png|jpe?g|gif|svg|woff2?|ico)(?:$|\?)", u, re.I):
                seen.add(u)
                out.append(u)
    return out
