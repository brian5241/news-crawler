"""네이버 뉴스스탠드 246개 언론사 전체에 자동 탐지(crawler.discover)를 돌려 결과를 집계한다.

출력: docs/discovery_results.csv (사이트별), docs/discovery_results.md (요약)
      --write-yaml 을 주면 수집 경로가 하나라도 있는 사이트를 sites.yaml 에 기록한다.
실행: python scripts/evaluate_discover.py [--workers 8] [--write-yaml] [--limit N]
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crawler.config import save_sites, upsert_site  # noqa: E402
from crawler.discover import discover_site  # noqa: E402
from crawler.fetch import make_client  # noqa: E402

CAT_NAME = {"ct1": "종합/방송/통신", "ct2": "경제/IT", "ct3": "스포츠/연예", "ct4": "매거진",
            "ct5": "영자/외국어", "ct6": "지역", "ct7": "전문지", "ct8": "인터넷"}
FIELDS = ["id", "name", "cat", "home", "final_home", "error", "rss_n", "sitemap_n", "news_sitemap_n",
          "pattern_n", "pattern_source", "list_n", "crawl_delay", "source", "sample", "elapsed", "notes"]


def load_targets(limit: int | None) -> list[dict]:
    cats: dict[str, str] = {}
    probes = sorted(glob.glob(os.path.join(ROOT, "docs", "site_probe_*.csv")))
    if probes:
        for r in csv.DictReader(open(probes[-1], encoding="utf-8-sig")):
            cats[r["id"]] = CAT_NAME.get(r["cat"], r["cat"])
    rows = []
    with open(os.path.join(ROOT, "docs", "newsstand_press_246.tsv"), encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3 or not parts[2] or parts[2] == "None":
                continue
            rows.append({"id": parts[0], "name": parts[1], "home": parts[2], "cat": cats.get(parts[0], "")})
    return rows[:limit] if limit else rows


def run_one(row: dict, client=None) -> tuple[dict, dict]:
    t0 = time.monotonic()
    with make_client(12.0) as own:   # 사이트마다 독립된 연결 풀(스레드 간 간섭 차단)
        cfg = discover_site(row["home"], name=row["name"], client=own)
    src = "rss" if cfg.rss else "news-sitemap" if cfg.news_sitemaps else "pattern" if cfg.article_patterns else "none"
    psrc = ""
    for n in cfg.notes:
        if n.startswith("기사 패턴 출처"):
            psrc = n.split(":")[1].split("(")[0].strip()
    out = dict(
        id=row["id"], name=row["name"], cat=row["cat"], home=row["home"], final_home=cfg.home, error=cfg.error or "",
        rss_n=len(cfg.rss), sitemap_n=len(cfg.sitemaps), news_sitemap_n=len(cfg.news_sitemaps),
        pattern_n=len(cfg.article_patterns), pattern_source=psrc, list_n=len(cfg.list_pages),
        crawl_delay=cfg.crawl_delay if cfg.crawl_delay is not None else "",
        source=src, sample=cfg.verified_samples[0] if cfg.verified_samples else "",
        elapsed=round(time.monotonic() - t0, 1), notes=" / ".join(cfg.notes),
    )
    return out, cfg.to_dict()


def pct(n: int, d: int) -> str:
    return f"{(100 * n / d):.0f}%" if d else "-"


def write_summary(results: list[dict], path: str) -> None:
    total = len(results)
    reach = [r for r in results if not r["error"]]
    with_src = [r for r in results if r["source"] != "none"]
    L = [f"# 사이트 자동 탐지 결과 ({time.strftime('%Y-%m-%d')})", "",
         f"대상: 네이버 뉴스스탠드 언론사 {total}곳. 입력은 홈페이지 주소 하나뿐이고 나머지는 전부 자동.", "",
         f"**수집 경로 확보 {len(with_src)}/{total} ({pct(len(with_src), total)})** "
         f"(RSS 또는 뉴스 사이트맵 또는 검증된 기사 URL 패턴 중 하나 이상)", "",
         "| 항목 | 사이트 수 | 비율 |", "|---|---|---|",
         f"| 홈페이지 접속 성공 | {len(reach)} | {pct(len(reach), total)} |",
         f"| RSS 피드 발견(검증됨) | {sum(1 for r in results if r['rss_n'])} | {pct(sum(1 for r in results if r['rss_n']), total)} |",
         f"| 사이트맵 발견 | {sum(1 for r in results if r['sitemap_n'])} | {pct(sum(1 for r in results if r['sitemap_n']), total)} |",
         f"| 뉴스 사이트맵 발견 | {sum(1 for r in results if r['news_sitemap_n'])} | {pct(sum(1 for r in results if r['news_sitemap_n']), total)} |",
         f"| 기사 URL 패턴 확보 | {sum(1 for r in results if r['pattern_n'])} | {pct(sum(1 for r in results if r['pattern_n']), total)} |",
         f"| 목록 페이지 후보 확보 | {sum(1 for r in results if r['list_n'])} | {pct(sum(1 for r in results if r['list_n']), total)} |",
         "", "## 1순위 수집 경로", "", "| 경로 | 사이트 수 |", "|---|---|"]
    for k, v in collections.Counter(r["source"] for r in results).most_common():
        L.append(f"| {k} | {v} |")
    L += ["", "## 기사 패턴을 어디서 얻었나", "", "| 출처 | 사이트 수 |", "|---|---|"]
    for k, v in collections.Counter(r["pattern_source"] or "-" for r in results).most_common():
        L.append(f"| {k} | {v} |")
    L += ["", "## 분류별", "", "| 분류 | 대상 | 수집 경로 확보 | 비율 |", "|---|---|---|---|"]
    cats = collections.Counter(r["cat"] for r in results)
    for c, n in sorted(cats.items(), key=lambda x: -x[1]):
        k = sum(1 for r in with_src if r["cat"] == c)
        L.append(f"| {c or '(미분류)'} | {n} | {k} | {pct(k, n)} |")
    el = [r["elapsed"] for r in results]
    unverified = sum(1 for r in results if "미검증" in r["notes"])
    L += ["", f"사이트당 소요: 평균 {sum(el) / len(el):.1f}초, 최대 {max(el):.1f}초" if el else "",
          f"기사 패턴 {sum(1 for r in results if r['pattern_n'])}곳 중 {unverified}곳은 샘플 추출 검증에 실패해 "
          "미검증 상태로 기록(대부분 JS 렌더링 사이트, 6단계 Playwright 대상).", ""]
    none = [r for r in results if r["source"] == "none"]
    L += [f"## 수집 경로를 못 찾은 {len(none)}곳", "", "| 언론사 | 홈 | 오류 | 메모 |", "|---|---|---|---|"]
    for r in none:
        L.append(f"| {r['name']} | {r['home']} | {r['error']} | {r['notes'][:120]} |")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--write-yaml", action="store_true", help="수집 경로가 있는 사이트를 sites.yaml 에 기록")
    ap.add_argument("--no-retry", action="store_true", help="일시적 오류 사이트 재시도 생략")
    ap.add_argument("--only-failed", action="store_true",
                    help="이전 결과(docs/discovery_results.csv)에서 수집 경로를 못 찾은 사이트만 다시 돌리고 결과를 합친다")
    ap.add_argument("--sites", default=os.path.join(ROOT, "sites.yaml"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    for name in ("trafilatura", "htmldate", "newspaper", "charset_normalizer", "httpx", "httpcore", "crawler"):
        logging.getLogger(name).setLevel(logging.ERROR)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    targets = load_targets(args.limit)
    previous: dict[str, dict] = {}
    if args.only_failed:
        prev_path = os.path.join(ROOT, "docs", "discovery_results.csv")
        for r in csv.DictReader(open(prev_path, encoding="utf-8-sig")):
            for k in ("rss_n", "sitemap_n", "news_sitemap_n", "pattern_n", "list_n"):
                r[k] = int(r[k] or 0)
            r["elapsed"] = float(r["elapsed"] or 0)
            previous[r["id"]] = r
        failed_ids = {i for i, r in previous.items() if r["source"] == "none"}
        targets = [t for t in targets if t["id"] in failed_ids]
    print(f"탐지 대상 {len(targets)}개", flush=True)
    results: list[dict] = []
    configs: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(run_one, row): row for row in targets}
        for i, fu in enumerate(as_completed(futs), 1):
            r, cfg = fu.result()
            results.append(r)
            configs.append(cfg)
            print(f"[{i}/{len(targets)}] {r['name']:<14} rss={r['rss_n']:<2} map={r['sitemap_n']:<2} "
                  f"news={r['news_sitemap_n']:<2} pat={r['pattern_n']:<2} list={r['list_n']:<3} "
                  f"{r['source']:<12} {r['elapsed']:>5}s {r['error'][:40]}", flush=True)
    # 일시적 오류(503, 타임아웃 등)로 실패한 사이트는 한 번 더 시도한다
    retry_rows = [row for row in targets if any(r["id"] == row["id"] and r["error"] and "403" not in r["error"]
                                                 for r in results)]
    if retry_rows and not args.no_retry:
        print(f"\n일시적 오류 사이트 {len(retry_rows)}곳 재시도", flush=True)
        time.sleep(3)
        with ThreadPoolExecutor(max_workers=min(4, args.workers)) as pool:
            for r, cfg in pool.map(run_one, retry_rows):
                idx = next(i for i, x in enumerate(results) if x["id"] == r["id"])
                if r["source"] != "none" or not r["error"]:
                    results[idx] = r
                    configs[idx] = cfg
                print(f"  재시도 {r['name']:<14} {r['source']:<12} {r['error'][:50]}", flush=True)
    if args.only_failed:   # 이전 결과에 이번 결과를 덮어 합친다
        merged = dict(previous)
        for r in results:
            merged[r["id"]] = r
        results = list(merged.values())
    results.sort(key=lambda r: r["id"])
    configs.sort(key=lambda c: c.get("domain", ""))
    csv_path = os.path.join(ROOT, "docs", "discovery_results.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(results)
    md_path = os.path.join(ROOT, "docs", "discovery_results.md")
    write_summary(results, md_path)
    if args.write_yaml:
        good = [c for c in configs if c.get("rss") or c.get("news_sitemaps") or c.get("article_patterns")]
        if args.only_failed:
            for c in good:
                upsert_site(c, args.sites)
            print(f"sites.yaml ← {len(good)}개 사이트 추가/교체: {args.sites}")
        else:
            save_sites(good, args.sites)
            print(f"sites.yaml ← {len(good)}개 사이트 기록: {args.sites}")
    ok = sum(1 for r in results if r["source"] != "none")
    print(f"\nDONE source-found={ok}/{len(results)} → {csv_path}, {md_path}")


if __name__ == "__main__":
    main()
