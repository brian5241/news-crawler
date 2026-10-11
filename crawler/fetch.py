"""HTTP 수집 계층.

- 브라우저형 기본 헤더, 리다이렉트 추적, 재시도(네트워크 오류·5xx·429)
- 응답 본문 크기 상한(max_bytes): 거대한 사이트맵·피드를 끝까지 받느라 매달리지 않도록 스트리밍으로 받다가 끊는다
- 한국 사이트에 흔한 EUC-KR/CP949 를 포함한 안전한 디코딩
  (utf-8 엄격 → 응답 헤더 charset → <meta charset> → cp949 → utf-8 replace 순)
"""
from __future__ import annotations

import codecs
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_BYTES = 5 * 1024 * 1024   # 응답 본문 상한. 기사 페이지는 보통 100KB~1MB, 사이트맵은 앞부분만 있어도 충분
DEFAULT_MAX_SECONDS = 45.0            # 요청 하나의 전체 벽시계 상한. 읽기 타임아웃은 '조용한' 시간만 재므로 찔끔찔끔 오는 응답은 따로 끊어야 한다
RETRY_DELAYS = (0.5, 1.5)  # 재시도 간격(초). 길이 = 최대 재시도 횟수
RETRY_STATUSES = frozenset({500, 502, 503, 504})   # 429(속도 제한)는 재시도하지 않는다. 더 두드리면 차단만 길어진다

# 봇 검사·차단 페이지의 흔적 (Datadome, Cloudflare, PerimeterX, Incapsula, Akamai 등)
_CHALLENGE_RE = re.compile(
    r"(captcha-delivery\.com|datadome|/cdn-cgi/challenge-platform|cf-chl|cf_chl_|Just a moment\.\.\.|Checking your browser"
    r"|px-captcha|perimeterx|_Incapsula_Resource|Incapsula incident|Request unsuccessful"
    r"|Please enable JS and disable any ad blocker|enable JavaScript and cookies to continue|<title>\s*Access Denied\s*</title>)",
    re.I,
)


def detect_block(status: int, head: str) -> tuple[Optional[str], bool]:
    """(이유, 확실한가). 봇 검사 페이지·429 는 확실한 차단(즉시 중단). 단순 403 은 특정 주소만 막힌 것일 수도 있어
    '약한' 신호로 돌려주고, 호출하는 쪽이 연속 횟수를 보고 판단한다."""
    m = _CHALLENGE_RE.search(head or "")
    if m:
        return f"bot challenge ({m.group(1)[:40]})", True
    if status == 429:
        return "HTTP 429 rate limited", True
    if status == 403:
        return "HTTP 403 forbidden", False
    return None, False


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None   # HTTP-date 형식은 무시

_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I)
_XML_DECL_RE = re.compile(rb"""<\?xml[^>]+encoding\s*=\s*["']([A-Za-z0-9_\-]+)""", re.I)
_ENCODING_ALIASES = {
    "euc-kr": "cp949", "euckr": "cp949", "ks_c_5601-1987": "cp949", "ksc5601": "cp949",
    "x-windows-949": "cp949", "windows-949": "cp949", "ms949": "cp949",
    "utf8": "utf-8", "utf-8": "utf-8",
}


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int = 0
    content: bytes = b""
    text: str = ""
    encoding: str = ""
    content_type: str = ""
    error: Optional[str] = None
    elapsed: float = 0.0
    truncated: bool = False   # max_bytes 에 걸려 앞부분만 받았음
    blocked: Optional[str] = None       # 봇 차단·속도 제한 응답이면 이유
    block_hard: bool = False            # True = 봇 검사 페이지·429 (확실), False = 단순 403 (약한 신호)
    retry_after: Optional[float] = None  # 서버가 알려준 대기 시간(초)

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300


def normalize_encoding(name: Optional[str]) -> Optional[str]:
    """charset 이름을 파이썬 코덱 이름으로. 모르는 이름이면 None."""
    if not name:
        return None
    n = name.strip().strip("\"'").lower()
    n = _ENCODING_ALIASES.get(n, n)
    try:
        codecs.lookup(n)
    except LookupError:
        return None
    return n


def sniff_encoding(content: bytes) -> Optional[str]:
    """HTML 앞부분의 <meta charset> 또는 XML 선언에서 인코딩을 읽는다."""
    head = content[:8192]
    m = _META_CHARSET_RE.search(head) or _XML_DECL_RE.search(head)
    if not m:
        return None
    return normalize_encoding(m.group(1).decode("ascii", "ignore"))


def decode_html(content: bytes, header_charset: Optional[str] = None) -> tuple[str, str]:
    """바이트를 문자열로 바꾼다. (text, 실제 사용한 인코딩) 반환.

    utf-8 엄격 디코딩을 먼저 시도하는 이유: 한글 페이지가 utf-8이면 다른 인코딩으로
    '우연히' 성공하는 일이 거의 없고, 반대로 cp949 페이지는 utf-8 엄격 디코딩에서 반드시 실패하기 때문.
    """
    if not content:
        return "", "utf-8"
    candidates: list[str] = []
    for enc in ("utf-8-sig", normalize_encoding(header_charset), sniff_encoding(content), "cp949"):
        if enc and enc not in candidates:
            candidates.append(enc)
    for enc in candidates:
        try:
            return content.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("utf-8", errors="replace"), "utf-8/replace"


def make_client(timeout: float = DEFAULT_TIMEOUT, verify: bool = True, **kwargs) -> httpx.Client:
    """여러 요청에 재사용할 클라이언트(연결 풀). 스레드 간 공유 가능."""
    return httpx.Client(headers=DEFAULT_HEADERS, timeout=timeout, follow_redirects=True, verify=verify, **kwargs)


def _read_capped(response: httpx.Response, max_bytes: int, deadline: float) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size >= max_bytes:
            return b"".join(chunks)[:max_bytes], True
        if time.monotonic() > deadline:   # 느리게 흘러오는 응답: 받은 데까지만 쓴다
            return b"".join(chunks), True
    return b"".join(chunks), False


def fetch(
    url: str,
    client: Optional[httpx.Client] = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    headers: Optional[dict] = None,
    retries: int = len(RETRY_DELAYS),
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_seconds: float = DEFAULT_MAX_SECONDS,
) -> FetchResult:
    """URL 하나를 받아 디코딩된 HTML까지 돌려준다. 예외를 밖으로 던지지 않는다."""
    own_client = client is None
    if own_client:
        client = make_client(timeout)
    hdrs = {**DEFAULT_HEADERS, **(headers or {})}
    started = time.monotonic()
    deadline = started + max_seconds
    last_error: Optional[str] = None
    got: Optional[tuple] = None   # (status, final_url, charset, content_type, content, truncated, retry_after)
    try:
        for attempt in range(retries + 1):
            try:
                with client.stream("GET", url, headers=hdrs, follow_redirects=True, timeout=timeout) as r:
                    status = r.status_code
                    ra = r.headers.get("retry-after")
                    if status in RETRY_STATUSES and attempt < retries:
                        got = (status, str(r.url), r.charset_encoding, r.headers.get("content-type", ""), b"", False, ra)
                    else:
                        content, truncated = _read_capped(r, max_bytes, deadline)
                        got = (status, str(r.url), r.charset_encoding, r.headers.get("content-type", ""), content, truncated, ra)
            except Exception as exc:  # 수집 계층은 어떤 오류에도 파이프라인을 죽이지 않는다
                got = None
                last_error = f"{type(exc).__name__}: {exc}"[:200]
                log.debug("fetch error %s (attempt %d): %s", url, attempt, last_error)
            if got is not None and got[0] not in RETRY_STATUSES:
                break
            if attempt < retries and time.monotonic() < deadline:
                time.sleep(RETRY_DELAYS[attempt])
            elif time.monotonic() >= deadline:
                break
        elapsed = time.monotonic() - started
        if got is None:
            return FetchResult(url=url, final_url=url, error=last_error or "unknown error", elapsed=elapsed)
        status, final_url, charset, ctype, content, truncated, ra = got
        text, enc = decode_html(content, charset)
        error = None if status < 400 else f"HTTP {status}"
        blocked, hard = detect_block(status, text[:30000])
        return FetchResult(url=url, final_url=final_url, status=status, content=content, text=text, encoding=enc,
                           content_type=ctype, error=error, elapsed=elapsed, truncated=truncated,
                           blocked=blocked, block_hard=hard, retry_after=_parse_retry_after(ra))
    finally:
        if own_client:
            client.close()
