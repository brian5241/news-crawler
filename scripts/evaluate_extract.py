"""1차 조사(docs/site_probe_*.csv)에서 확보한 기사 URL로 crawler.extract 품질을 일괄 평가한다.

출력: docs/extract_quality.csv (사이트별 상세), docs/extract_quality.md (요약)
실행: python scripts/evaluate_extract.py [--workers 8]

성공률은 '기사 URL'로 분류된 사이트만으로 계산한다. 1차 조사 스크립트가 CSS·이미지·목록·태그 페이지를
기사로 잘못 고른 경우는 추출기의 책임이 아니므로 따로 표에 적는다(2단계 탐지 모듈에서 해결).
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crawler.extract import FALLBACK_MIN_LEN, MIN_TEXT_LEN, _run_trafilatura, clean_text, extract_from_html  # noqa: E402
from crawler.fetch import fetch, make_client  # noqa: E402

CAT_NAME = {"ct1": "종합/방송/통신", "ct2": "경제/IT", "ct3": "스포츠/연예", "ct4": "매거진",
            "ct5": "영자/외국어", "ct6": "지역", "ct7": "전문지", "ct8": "인터넷"}
FIELDS = ["id", "name", "cat", "url", "url_kind", "status", "encoding", "extractor", "ok", "title", "text_length",
          "len_precision", "len_recall", "date", "author", "publisher", "error", "elapsed"]
_NON_ARTICLE_RE = re.compile(
    r"(\.(?:css|js|jpe?g|png|gif|svg|webp|xml|pdf)(?:\?|$)|/rss/?(?:$|\?)|wp-json|/list/|ad_view|members-event"
    r"|/null/|/tag/|/category/|/issue/articles/|/css/|/img/|/assets/|/_next/|/thumb/)", re.I)


def load_targets() -> list[dict]:
    files = sorted(glob.glob(os.path.join(ROOT, "docs", "site_probe_*.csv")))
    if not files:
        sys.exit("docs/site_probe_*.csv 가 없습니다")
    rows = list(csv.DictReader(open(files[-1], encoding="utf-8-sig")))
    return [r for r in rows if r.get("article")]


def evaluate(row: dict, client) -> dict:
    url = row["article"]
    out = {k: "" for k in FIELDS}
    out.update(id=row["id"], name=row["name"], cat=CAT_NAME.get(row["cat"], row["cat"]), url=url,
               url_kind="non-article" if _NON_ARTICLE_RE.search(url) else "article")
    t0 = time.monotonic()
    res = fetch(url, client=client)
    out["status"] = res.status
    out["encoding"] = res.encoding
    if not res.ok:
        out.update(extractor="none", ok=False, error=res.error, elapsed=round(time.monotonic() - t0, 2))
        return out
    art = extract_from_html(res.text, url, final_url=res.final_url, http_status=res.status, encoding=res.encoding)
    p = _run_trafilatura(res.text, url) or {}
    r = _run_trafilatura(res.text, url, favor_recall=True) or {}
    out.update(
        extractor=art.extractor, ok=art.ok, title=(art.title or "")[:80], text_length=art.text_length,
        len_precision=len(clean_text(p.get("text"))), len_recall=len(clean_text(r.get("text"))),
        date=art.date or "", author=art.author or "", publisher=art.publisher or "", error=art.error or "",
        elapsed=round(time.monotonic() - t0, 2),
    )
    return out


def pct(n: int, d: int) -> str:
    return f"{(100 * n / d):.0f}%" if d else "-"


def write_summary(results: list[dict], path: str) -> None:
    arts = [r for r in results if r["url_kind"] == "article"]
    non = [r for r in results if r["url_kind"] == "non-article"]
    ok = [r for r in arts if r["ok"] is True]
    fail = [r for r in arts if r["ok"] is not True]
    by_ext = collections.Counter(r["extractor"] for r in arts)
    enc = collections.Counter(r["encoding"] for r in arts if r["encoding"])
    L = [f"# 기사 추출 모듈 품질 평가 ({time.strftime('%Y-%m-%d')})", "",
         f"대상: 1차 조사에서 URL을 확보한 {len(results)}개 사이트, 사이트당 1건. "
         f"이 중 기사 URL {len(arts)}곳으로 성공률을 계산하고, 기사가 아닌 링크 {len(non)}곳은 맨 아래 따로 적는다.",
         f"기준: 본문 {MIN_TEXT_LEN}자 이상이면 성공 ({FALLBACK_MIN_LEN}자 미만이면 다음 추출기도 시도).", "",
         f"**기사 URL 기준 성공 {len(ok)}/{len(arts)} ({pct(len(ok), len(arts))})**", "",
         "## 어느 단계에서 뽑혔나 (기사 URL 기준)", "", "| 추출기 | 사이트 수 |", "|---|---|"]
    for k, v in by_ext.most_common():
        L.append(f"| {k} | {v} |")
    L += ["", "`*:short` = 모든 추출기가 200자 미만이라 가장 긴 결과를 채택(포토 기사 등). "
              "`meta-only` = 본문 없음, 메타데이터만.", "",
          "## 성공한 기사의 메타데이터 채움률", "", "| 항목 | 채움 | 비율 |", "|---|---|---|"]
    for label, key, cond in (("제목", "title", lambda v: bool(v)), ("날짜", "date", lambda v: bool(v)),
                             ("날짜(시각 포함)", "date", lambda v: bool(v) and ":" in v),
                             ("기자", "author", lambda v: bool(v)),
                             ("언론사명(도메인 아닌 이름)", "publisher", lambda v: bool(v) and "." not in v)):
        n = sum(1 for r in ok if cond(r[key]))
        L.append(f"| {label} | {n}/{len(ok)} | {pct(n, len(ok))} |")
    L += ["", "## 페이지 인코딩 분포 (기사 URL 기준)", "", "| 인코딩 | 사이트 수 |", "|---|---|"]
    for k, v in enc.most_common():
        L.append(f"| {k} | {v} |")
    L += ["", "`utf-8/replace` = utf-8·선언 charset·cp949 엄격 디코딩이 모두 실패해 깨진 바이트만 치환한 경우.", "",
          "## 분류별 (기사 URL 기준)", "", "| 분류 | 대상 | 성공 | 비율 |", "|---|---|---|---|"]
    cats = collections.Counter(r["cat"] for r in arts)
    for c, n in sorted(cats.items(), key=lambda x: -x[1]):
        k = sum(1 for r in ok if r["cat"] == c)
        L.append(f"| {c} | {n} | {k} | {pct(k, n)} |")
    gap = [r for r in ok if r["extractor"] == "trafilatura" and r["len_recall"] and r["len_precision"]
           and r["len_recall"] >= 2 * r["len_precision"]]
    L += ["", f"## 정밀도 모드와 재현율 모드 길이 차이가 2배 이상인 곳 ({len(gap)}곳, 수동 확인 후보)", ""]
    for r in gap:
        L.append(f"- {r['name']}: 정밀 {r['len_precision']}자 / 재현 {r['len_recall']}자 · {r['url']}")
    L += ["", f"## 추출 실패 {len(fail)}곳 (기사 URL인데 본문을 못 뽑음)", "",
          "| 언론사 | HTTP | 추출기 | 본문길이 | 오류 | URL |", "|---|---|---|---|---|---|"]
    for r in fail:
        L.append(f"| {r['name']} | {r['status']} | {r['extractor']} | {r['text_length']} | {r['error']} | {r['url']} |")
    L += ["", f"## 기사 아닌 URL {len(non)}곳 (1차 조사 스크립트의 링크 선택 문제, 2단계 탐지 모듈에서 해결)", "",
          "성공률 계산에서 제외. 목록·태그 페이지는 본문이 '뽑히긴' 하지만 기사가 아니다.", "",
          "| 언론사 | 추출기 | 본문길이 | URL |", "|---|---|---|---|"]
    for r in non:
        L.append(f"| {r['name']} | {r['extractor']} | {r['text_length']} | {r['url']} |")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    for name in ("trafilatura", "htmldate", "newspaper", "charset_normalizer", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.ERROR)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    targets = load_targets()
    print(f"평가 대상 {len(targets)}개", flush=True)
    results: list[dict] = []
    with make_client() as client, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(evaluate, row, client): row for row in targets}
        for i, fu in enumerate(as_completed(futs), 1):
            r = fu.result()
            results.append(r)
            print(f"[{i}/{len(targets)}] {r['name']:<14} {str(r['status']):<4} {r['extractor']:<20} "
                  f"{str(r['text_length']):<6} {'OK' if r['ok'] is True else 'FAIL'}", flush=True)
    results.sort(key=lambda r: r["id"])
    csv_path = os.path.join(ROOT, "docs", "extract_quality.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(results)
    md_path = os.path.join(ROOT, "docs", "extract_quality.md")
    write_summary(results, md_path)
    arts = [r for r in results if r["url_kind"] == "article"]
    ok = sum(1 for r in arts if r["ok"] is True)
    print(f"\nDONE article-url ok={ok}/{len(arts)} (non-article urls: {len(results) - len(arts)}) → {csv_path}, {md_path}")


if __name__ == "__main__":
    main()
