"""등록된 사이트마다 기사 N건을 같은 세션(쿠키 유지)으로 '순서대로' 읽어 유료·로그인 벽을 찾는다.

찾는 것
  - 계량(metered) 페이월: 뉴욕타임스처럼 처음 몇 건은 보이다가 그 뒤로 막히는 경우.
    결정적 시험 = N건을 읽은 같은 세션에서 '첫 기사'를 다시 읽어, 처음엔 열렸던 기사가 잠겼는지 본다.
  - 부분 잠금: 일부 기사만 로그인·구독을 요구하는 경우 (한국 언론사의 흔한 형태)
신호
  - JSON-LD isAccessibleForFree=false (구글용 표준 표시), 로그인 페이지로 리다이렉트, HTTP 401/402/403
  - 본문이 짧은데 페이지에 paywall/login-wall 류 DOM 표식, 유료회원전용 류 강한 문구, 본문 속 로그인 후 이용 류 문구가 있음
  - 본문이 og:description(요약)과 같은 수준으로만 보이는 '티저'
오탐 방지
  - 어떤 표식·문구가 그 사이트의 '열린' 긴 기사에도 똑같이 나오면 사이트 공통 메뉴(chrome)로 보고 잠금 판단에서 뺀다.
한계
  - 자바스크립트로만 동작하는 클라이언트 측 계량(NYT 방식)은 실제 브라우저 없이는 보이지 않는다.
출력: docs/paywall_check.csv (기사별), docs/paywall_check_sites.csv (사이트별), docs/paywall_check.md (요약)
실행: python scripts/check_paywall.py [--articles 8] [--workers 8] [--gap 1.0] [--limit N] [--only dom1,dom2]
      --only 는 지정한 도메인만 다시 돌리고 이전 결과와 합친다.
"""
from __future__ import annotations

import argparse
import collections
import csv
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crawler.config import load_sites  # noqa: E402
from crawler.extract import _jsonld_nodes, _parse, extract_from_html, looks_like_html  # noqa: E402
from crawler.fetch import make_client  # noqa: E402
from crawler.sources import Collector  # noqa: E402

LOGIN_URL_RE = re.compile(r"(login|signin|sign-in|/member/|membership|subscri|paywall|/auth/|join)", re.I)
PAYWALL_DOM_RE = re.compile(
    r"""(?:class|id)\s*=\s*["'][^"']*(paywall|pay-wall|pay_wall|loginwall|login-wall|login_wall|subscribe-wall|premium-wall"""
    r"""|article-lock|article_lock|locked-content|content-lock|membership-only|members-only|blur-content|read-more-gate"""
    r"""|gate-content|metered|meter-|piano-|tp-modal|regwall|reg-wall)[^"']*["']""", re.I)
PAYWALL_TEXT_RE = re.compile(
    r"(로그인\s*(?:후|하시면|이\s*필요|하고\s*계속|해야)|구독\s*(?:후|하시면|자\s*전용|회원\s*전용|이\s*필요|하고\s*계속)"
    r"|유료\s*(?:회원|구독|기사|콘텐츠)|멤버십\s*(?:회원|전용|가입)|회원\s*전용|프리미엄\s*(?:회원|구독|기사|콘텐츠)"
    r"|이어서\s*보려면|이어서\s*읽|전문을?\s*보(?:시)?려면|기사\s*전문은|남은\s*(?:무료\s*)?기사|무료\s*기사\s*\d"
    r"|이번\s*달\s*무료|무료로\s*읽을\s*수\s*있는|열람\s*권한|구독자만|회원만\s*볼|로그인이\s*필요"
    r"|subscribe to continue|subscribers? only|sign in to continue|free articles? (?:left|remaining)|premium content"
    r"|this article is for subscribers)", re.I)
# 페이지 HTML 어디에 있든 '잠금'을 뜻하는 강한 문구 (사이트 공통 메뉴에는 잘 안 나오는 표현만)
PAYWALL_HTML_RE = re.compile(
    r"(유료\s*회원\s*전용|유료회원만|유료\s*회원만\s*열람|유료\s*기사입니다|이\s*기사는\s*유료|구독자\s*전용\s*기사|회원\s*전용\s*기사"
    r"|로그인\s*또는\s*회원가입을\s*해\s*주세요|후원자는\s*로그인\s*후|정기구독자(?:만|는)\s*(?:볼|열람)|전문은\s*구독|전문을\s*보시려면\s*(?:로그인|구독)"
    r"|会員限定|有料会員|続きを読むには|ログインが必要|有料記事"
    r"|this article is for subscribers|subscribers? only|subscribe to (?:read|continue)|you've reached your (?:free )?(?:article )?limit)", re.I)
STRONG = ("isAccessibleForFree=false", "login-redirect")
GATES = ("paywall-dom", "paywall-html", "paywall-text")
ROW_FIELDS = ["domain", "name", "idx", "url", "status", "final_url", "ok", "extractor", "text_length", "html_len", "signals", "title"]
SITE_FIELDS = ["domain", "name", "n", "locked", "locked_positions", "chrome", "short", "paywall_dom", "klass", "recheck",
               "sample", "signals", "elapsed"]
INT_SITE_FIELDS = ("n", "locked", "short", "paywall_dom")


def jsonld_not_free(tree) -> bool:
    for n in _jsonld_nodes(tree):
        if n.get("isAccessibleForFree") in (False, "False", "false", 0, "0"):
            return True
    return False


def detect(res, art, tree) -> list[str]:
    sig: list[str] = []
    if res.status in (401, 402, 403):
        sig.append(f"http{res.status}")
    path_q = urlsplit(res.final_url).path + "?" + urlsplit(res.final_url).query
    if LOGIN_URL_RE.search(path_q) and not LOGIN_URL_RE.search(urlsplit(res.url).path):
        sig.append("login-redirect")
    if tree is not None and jsonld_not_free(tree):
        sig.append("isAccessibleForFree=false")
    if PAYWALL_DOM_RE.search(res.text or ""):
        sig.append("paywall-dom")
    if PAYWALL_HTML_RE.search(res.text or ""):
        sig.append("paywall-html")
    if art is not None and art.text and PAYWALL_TEXT_RE.search(art.text):
        sig.append("paywall-text")
    if art is not None:
        if art.text_length < 300:
            sig.append("short")
        desc = (art.description or "").replace(" ", "")
        txt = (art.text or "").replace(" ", "")
        if art.text_length < 600 and desc and txt and (txt[:60] in desc or desc[:60] in txt):
            sig.append("teaser-only")
    return sig


def is_locked(sig: list[str], chrome: set[str] = frozenset()) -> bool:
    if any(s in STRONG for s in sig) or any(s.startswith("http4") for s in sig):
        return True
    gate = any(g in sig and g not in chrome for g in GATES)
    return gate and ("short" in sig or "teaser-only" in sig)


def chrome_signals(rows: list[dict]) -> set[str]:
    """열린 긴 기사에도 똑같이 나오는 표식·문구는 사이트 공통 요소로 본다."""
    out: set[str] = set()
    if not rows:
        return out
    open_rows = [r for r in rows if r["text_length"] >= 600 and not any(s in STRONG for s in r["signals"].split("|"))]
    for g in GATES:
        hits = [r for r in rows if g in r["signals"].split("|")]
        if len(hits) >= 0.8 * len(rows) and any(g in r["signals"].split("|") for r in open_rows):
            out.add(g)
    return out


def _row(site, idx, u, res, art, sig, title=None) -> dict:
    return dict(domain=site["domain"], name=site.get("name", ""), idx=idx, url=u, status=res.status,
                final_url=res.final_url, ok=bool(art and art.ok), extractor=art.extractor if art else "none",
                text_length=art.text_length if art else 0, html_len=len(res.content or b""), signals="|".join(sig),
                title=title if title is not None else ((art.title or "")[:60] if art else ""))


def check_site(site: dict, articles: int, gap: float, budget: float) -> tuple[dict, list[dict]]:
    t0 = time.monotonic()
    rows: list[dict] = []
    recheck_row = None
    with make_client(12.0) as client:   # 사이트당 세션 하나: 쿠키가 유지돼 계량 페이월이 작동할 수 있다
        col = Collector(client=client, gap=gap)
        cands = col.collect(site, limit=articles + 4)
        urls = [c["url"] for c in cands][:articles]
        for i, u in enumerate(urls, 1):
            if time.monotonic() - t0 > budget:
                break
            res = col._get(u)
            art = tree = None
            if res.ok and looks_like_html(res.text):
                art = extract_from_html(res.text, u, final_url=res.final_url, http_status=res.status)
                tree = _parse(res.text)
            rows.append(_row(site, i, u, res, art, detect(res, art, tree)))
        # 계량 페이월의 결정적 시험: 같은 세션에서 첫 기사를 다시 읽는다
        if rows and rows[0]["text_length"] >= 300 and not is_locked(rows[0]["signals"].split("|")):
            u = rows[0]["url"]
            res = col._get(u)
            art = tree = None
            if res.ok and looks_like_html(res.text):
                art = extract_from_html(res.text, u, final_url=res.final_url, http_status=res.status)
                tree = _parse(res.text)
            recheck_row = _row(site, 0, u, res, art, detect(res, art, tree), title="(첫 기사 재확인)")
    n = len(rows)
    chrome = chrome_signals(rows)
    locked = [r["idx"] for r in rows if is_locked(r["signals"].split("|"), chrome)]
    short = sum(1 for r in rows if "short" in r["signals"].split("|"))
    dom = sum(1 for r in rows if "paywall-dom" in r["signals"].split("|"))
    recheck = ""
    metered = False
    if recheck_row is not None:
        rl = is_locked(recheck_row["signals"].split("|"), chrome)
        first = rows[0]
        shrunk = (recheck_row["text_length"] < 0.4 * first["text_length"]
                  and recheck_row["html_len"] < 0.7 * first["html_len"])   # 추출기 흔들림이 아니라 페이지 자체가 줄었을 때만
        metered = rl or shrunk
        recheck = "잠김" if metered else f"열림 {recheck_row['text_length']}자"
        rows.append(recheck_row)
    if n == 0:
        klass = "no-articles"
    elif metered:
        klass = "metered-confirmed"
    elif not locked:
        klass = "js-or-empty" if short >= max(1, int(n * 0.8)) else "free"
    else:
        klass = "partial-lock"
    main_rows = [r for r in rows if r["idx"] > 0]
    summary = dict(domain=site["domain"], name=site.get("name", ""), n=n, locked=len(locked),
                   locked_positions=",".join(map(str, locked)), chrome="|".join(sorted(chrome)), short=short,
                   paywall_dom=dom, klass=klass, recheck=recheck,
                   sample=next((r["url"] for r in main_rows if r["idx"] in locked), main_rows[0]["url"] if main_rows else ""),
                   signals="|".join(sorted({s for r in main_rows for s in r["signals"].split("|") if s})),
                   elapsed=round(time.monotonic() - t0, 1))
    return summary, rows


def write_md(summaries: list[dict], path: str, articles: int) -> None:
    c = collections.Counter(s["klass"] for s in summaries)
    rechecked = sum(1 for s in summaries if s.get("recheck"))
    L = [f"# 유료·로그인 벽 점검 ({time.strftime('%Y-%m-%d')})", "",
         f"sites.yaml 의 {len(summaries)}개 사이트마다 최신 기사 최대 {articles}건을 같은 세션(쿠키 유지)으로 순서대로 읽고, "
         f"마지막에 **첫 기사를 같은 세션에서 다시 읽어** 처음엔 열렸던 기사가 잠겼는지 확인했다(계량 페이월의 결정적 시험, {rechecked}곳 수행).", "",
         "| 판정 | 사이트 수 | 뜻 |", "|---|---|---|",
         f"| free | {c['free']} | 읽은 기사 전부 본문이 나왔고 잠금 신호 없음. 첫 기사 재확인도 열림 |",
         f"| partial-lock | {c['partial-lock']} | 일부 기사만 잠김 (특정 기사에 로그인·구독 요구, 한국 언론사의 흔한 형태). 재확인은 열림 |",
         f"| metered-confirmed | {c['metered-confirmed']} | 처음 열렸던 첫 기사가 {articles}건 읽은 뒤 잠김 (뉴욕타임스식 계량 페이월) |",
         f"| js-or-empty | {c['js-or-empty']} | 대부분 본문이 짧음. 페이월이 아니라 JS 렌더링 사이트일 가능성 (6단계 Playwright 대상) |",
         f"| no-articles | {c['no-articles']} | 기사 주소를 모으지 못함 |", "",
         "잠금 신호: `isAccessibleForFree=false`(JSON-LD 표준 표시), `login-redirect`, `http401/402/403`, 또는 "
         "`paywall-dom`(paywall/login-wall/piano 류 표식)·`paywall-html`(유료회원전용·会員限定 류 강한 문구)·"
         "`paywall-text`(본문 안의 로그인 후·구독하시면 류 문구)가 짧은 본문·티저와 함께 나타날 때. "
         "그 사이트의 열린 긴 기사에도 똑같이 나오는 표식·문구는 공통 메뉴로 보고 제외했다(`chrome` 열).", "",
         "**한계**: 자바스크립트로만 동작하는 클라이언트 측 계량(NYT 방식)은 실제 브라우저 없이는 보이지 않는다. "
         "여기서 'free'는 서버가 HTML로 본문을 내려줬다는 뜻이다.", ""]
    flagged = [s for s in summaries if s["klass"] in ("metered-confirmed", "partial-lock")]
    L += [f"## 잠금 신호가 나온 {len(flagged)}곳", "",
          "| 언론사 | 판정 | 읽은 수 | 잠긴 위치 | 첫 기사 재확인 | 신호 | 공통 메뉴로 제외 | 샘플 |", "|---|---|---|---|---|---|---|---|"]
    for s in sorted(flagged, key=lambda x: (x["klass"], -x["locked"])):
        L.append(f"| {s['name']} | {s['klass']} | {s['n']} | {s['locked_positions']} | {s.get('recheck', '')} | {s['signals']} | {s.get('chrome', '')} | {s['sample']} |")
    markup = [s for s in summaries if (s["paywall_dom"] or "paywall-html" in s["signals"]) and s["klass"] == "free"]
    L += ["", f"## 본문은 열리지만 페이월 장치나 유료 안내 문구가 있는 {len(markup)}곳 (프리미엄 기사만 잠그는 곳일 수 있음)", ""]
    for s in markup:
        L.append(f"- {s['name']} ({s['domain']}): paywall 표식 {s['paywall_dom']}/{s['n']}건, 신호 {s['signals']}")
    js = [s for s in summaries if s["klass"] in ("js-or-empty", "no-articles")]
    L += ["", f"## 판정 불가 {len(js)}곳", ""]
    for s in js:
        L.append(f"- {s['name']} ({s['domain']}): {s['klass']}, 읽은 수 {s['n']}, 짧은 본문 {s['short']}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def _load_previous(rows_path: str, sites_path: str) -> tuple[list[dict], list[dict]]:
    rows, sites = [], []
    if os.path.exists(rows_path):
        for r in csv.DictReader(open(rows_path, encoding="utf-8-sig")):
            r["idx"] = int(r["idx"] or 0)
            r["text_length"] = int(r["text_length"] or 0)
            r["html_len"] = int(r.get("html_len") or 0)
            rows.append(r)
    if os.path.exists(sites_path):
        for s in csv.DictReader(open(sites_path, encoding="utf-8-sig")):
            for k in INT_SITE_FIELDS:
                s[k] = int(s[k] or 0)
            s["elapsed"] = float(s["elapsed"] or 0)
            sites.append(s)
    return rows, sites


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--articles", type=int, default=8)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--gap", type=float, default=1.0, help="같은 사이트 안에서 요청 간격(초)")
    ap.add_argument("--budget", type=float, default=70.0, help="사이트당 최대 시간(초)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only", default=None, help="쉼표로 구분한 도메인만 다시 돌리고 이전 결과와 합친다")
    ap.add_argument("--sites", default=os.path.join(ROOT, "sites.yaml"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    for name in ("trafilatura", "htmldate", "newspaper", "charset_normalizer", "httpx", "httpcore", "crawler"):
        logging.getLogger(name).setLevel(logging.ERROR)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    rows_path = os.path.join(ROOT, "docs", "paywall_check.csv")
    sites_path = os.path.join(ROOT, "docs", "paywall_check_sites.csv")
    md_path = os.path.join(ROOT, "docs", "paywall_check.md")

    sites = load_sites(args.sites)
    only = {d.strip() for d in args.only.split(",")} if args.only else None
    if only:
        sites = [s for s in sites if s["domain"] in only]
    if args.limit:
        sites = sites[:args.limit]
    print(f"점검 대상 {len(sites)}개 사이트, 사이트당 기사 {args.articles}건", flush=True)
    summaries: list[dict] = []
    all_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(check_site, s, args.articles, args.gap, args.budget): s for s in sites}
        for i, fu in enumerate(as_completed(futs), 1):
            summary, rows = fu.result()
            summaries.append(summary)
            all_rows.extend(rows)
            print(f"[{i}/{len(sites)}] {summary['name']:<14} n={summary['n']:<2} locked={summary['locked']:<2} "
                  f"short={summary['short']:<2} {summary['klass']:<18} {summary['elapsed']:>5}s {summary['signals'][:60]}", flush=True)
    if only:
        prev_rows, prev_sites = _load_previous(rows_path, sites_path)
        done = {s["domain"] for s in summaries}
        all_rows = [r for r in prev_rows if r["domain"] not in done] + all_rows
        summaries = [s for s in prev_sites if s["domain"] not in done] + summaries
    summaries.sort(key=lambda s: s["domain"])
    all_rows.sort(key=lambda r: (r["domain"], r["idx"]))
    with open(rows_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=ROW_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_rows)
    with open(sites_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=SITE_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(summaries)
    write_md(summaries, md_path, args.articles)
    c = collections.Counter(s["klass"] for s in summaries)
    print(f"\nDONE {dict(c)} → {rows_path}, {sites_path}, {md_path}")


if __name__ == "__main__":
    main()
