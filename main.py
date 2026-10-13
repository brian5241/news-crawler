"""news-crawler CLI.

  add-site DOMAIN [--name N]      자동 탐지 후 sites.yaml 에 추가(같은 도메인이면 교체)   ← 새 사이트 등록은 이 한 줄
  discover DOMAIN                 자동 탐지 결과만 출력
  list-sites                      sites.yaml 에 등록된 사이트 표
  crawl [--only d1,d2] [--limit-per-site 30]   등록된 사이트에서 새 기사를 받아 SQLite(data/news.db)에 저장
  export [--format csv|jsonl] [--site D] [--since YYYY-MM-DD]   저장된 기사를 파일로
  stats                           DB 현황과 마지막 실행 요약
  extract URL [--json]            URL 하나에서 기사 추출(디버그용)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

import yaml

from crawler.config import DEFAULT_PATH, load_sites, select_sites, site_key, upsert_site
from crawler.discover import discover_site
from crawler.extract import extract_article
from crawler.pipeline import Crawler, write_report
from crawler.storage import DEFAULT_DB, Storage
from crawler.commoncrawl import crawl_domain as cc_crawl_domain, search_index as cc_search, list_indices as cc_list_indices

NOISY_LOGGERS = ("trafilatura", "htmldate", "newspaper", "charset_normalizer", "httpx", "httpcore", "crawler")


def _setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.ERROR)


# ---------------------------------------------------------------- extract
def cmd_extract(args: argparse.Namespace) -> int:
    if getattr(args, "js", False):
        from crawler.browser import BrowserFetcher
        with BrowserFetcher(timeout=args.timeout) as bf:
            res = bf.fetch(args.url)
        if not res.ok:
            print(f"오류: {res.error}")
            return 1
        from crawler.extract import extract_from_html
        art = extract_from_html(res.text, args.url, final_url=res.final_url,
                                http_status=res.status, encoding=res.encoding)
    else:
        art = extract_article(args.url, timeout=args.timeout)
    if args.json:
        print(json.dumps(art.to_dict(), ensure_ascii=False, indent=2))
        return 0 if art.ok else 1
    print(f"URL      : {art.final_url}")
    print(f"상태     : HTTP {art.http_status}  인코딩 {art.encoding}  추출기 {art.extractor}  ok={art.ok}")
    if art.error:
        print(f"오류     : {art.error}")
    print(f"언론사   : {art.publisher}")
    print(f"제목     : {art.title}")
    print(f"날짜     : {art.date}")
    print(f"기자     : {art.author}")
    print(f"본문({art.text_length}자)")
    if art.text:
        preview = art.text if len(art.text) <= args.preview else art.text[: args.preview] + " …"
        print(preview)
    return 0 if art.ok else 1


# ---------------------------------------------------------------- discover / add-site / list-sites
def _print_discovery(cfg) -> None:
    print(f"사이트   : {cfg.name} ({cfg.domain})  홈 {cfg.home}")
    if cfg.error:
        print(f"오류     : {cfg.error}")
    print(f"RSS      : {len(cfg.rss)}개" + (f"  예) {cfg.rss[0]}" if cfg.rss else ""))
    print(f"사이트맵 : {len(cfg.sitemaps)}개 (뉴스 사이트맵 {len(cfg.news_sitemaps)}개)")
    print(f"기사 패턴: {len(cfg.article_patterns)}개")
    for p, s in zip(cfg.article_patterns, cfg.verified_samples):
        print(f"           {p}\n           샘플 {s}")
    print(f"목록 페이지 후보: {len(cfg.list_pages)}개")
    for n in cfg.notes:
        print(f"  - {n}")
    print(f"수집 가능: {'예' if cfg.has_source else '아니오'}")


def _discover_maybe_llm(args: argparse.Namespace):
    """--llm 이면 자동 탐지가 비었을 때 Claude 제안을 받아 검증한다. (cfg, prompt_or_None)"""
    if not getattr(args, "llm", False):
        return discover_site(args.domain, name=args.name, verify=not args.no_verify), None
    from crawler.llm_assist import LLMUnavailable, assist_site
    try:
        return assist_site(args.domain, name=args.name, dry_run=getattr(args, "dry_run", False),
                           force=getattr(args, "llm_force", False))
    except LLMUnavailable as exc:
        print(f"LLM 보조를 쓸 수 없습니다: {exc}")
        print("자격 증명 없이 보낼 내용만 보려면 --dry-run 을 붙이세요.")
        return discover_site(args.domain, name=args.name, verify=not args.no_verify), None


def cmd_discover(args: argparse.Namespace) -> int:
    cfg, prompt = _discover_maybe_llm(args)
    if args.yaml:
        print(yaml.safe_dump({"sites": [cfg.to_dict()]}, allow_unicode=True, sort_keys=False, width=200))
    else:
        _print_discovery(cfg)
    if prompt and getattr(args, "dry_run", False):
        print("\n--- Claude 에 보낼 프롬프트 (dry-run, 실제 호출 안 함) ---")
        print(prompt[:4000] + (" …" if len(prompt) > 4000 else ""))
    return 0 if cfg.has_source else 1


def cmd_add_site(args: argparse.Namespace) -> int:
    cfg, _ = _discover_maybe_llm(args)
    _print_discovery(cfg)
    if not cfg.has_source and not args.force:
        print("수집 경로를 하나도 못 찾아 저장하지 않았습니다. (--force 로 강제 저장)")
        return 1
    replaced = upsert_site(cfg.to_dict(), args.sites)
    print(f"{'교체' if replaced else '추가'}: {args.sites} ← {site_key(cfg.to_dict())}")
    return 0


def cmd_list_sites(args: argparse.Namespace) -> int:
    sites = load_sites(args.sites)
    if not sites:
        print(f"{args.sites} 에 등록된 사이트가 없습니다. 먼저: python main.py add-site 도메인")
        return 1
    print(f"{'이름':<16} {'키(홈 호스트)':<28} {'RSS':>4} {'맵':>3} {'뉴스맵':>5} {'패턴':>4} {'목록':>4}  탐지일")
    for s in sites:
        print(f"{s.get('name', '')[:16]:<16} {site_key(s)[:28]:<28} {len(s.get('rss') or []):>4} "
              f"{len(s.get('sitemaps') or []):>3} {len(s.get('news_sitemaps') or []):>5} "
              f"{len(s.get('article_patterns') or []):>4} {len(s.get('list_pages') or []):>4}  {s.get('discovered_at', '')}")
    print(f"총 {len(sites)}개")
    return 0


# ---------------------------------------------------------------- crawl / export / stats
def _select_sites(args: argparse.Namespace) -> list[dict]:
    sites = load_sites(args.sites)
    if getattr(args, "only", None):
        wanted = {d.strip() for d in args.only.split(",") if d.strip()}
        sites = select_sites(sites, wanted)
    return sites


def cmd_crawl(args: argparse.Namespace) -> int:
    sites = _select_sites(args)
    if not sites:
        print("수집할 사이트가 없습니다. (sites.yaml 비었거나 --only 에 맞는 사이트 없음)")
        return 1
    strategies = tuple(s.strip() for s in args.strategies.split(",") if s.strip())
    t0 = time.monotonic()
    print(f"수집 시작: 사이트 {len(sites)}개, 사이트당 최대 {args.limit_per_site}건, 작업자 {args.workers}, "
          f"요청 간격 {args.gap}초, robots {'무시' if args.no_robots else '준수'} → {args.db}", flush=True)

    def progress(i: int, n: int, r) -> None:
        print(f"[{i}/{n}] {r.name:<14} 주소 {r.found:<3} 새 {r.new:<3} 저장 {r.stored:<3} 중복 {r.duplicates:<2} "
              f"실패 {r.failed:<2} robots {r.skipped_robots:<2} {r.elapsed:>6}s {r.note}", flush=True)

    def run_once() -> None:
        t1 = time.monotonic()
        with Storage(args.db) as storage:
            crawler = Crawler(storage, limit_per_site=args.limit_per_site, gap=args.gap, timeout=args.timeout,
                              respect_robots=not args.no_robots, site_budget=args.site_budget, strategies=strategies)
            summary = crawler.crawl(sites, workers=args.workers, progress=progress)
            tot = storage.totals()
            print(f"\n완료 ({time.monotonic() - t1:.0f}초): 주소 {summary.total('found')}, 새 주소 {summary.total('new')}, "
                  f"저장 {summary.total('stored')}, 중복 {summary.total('duplicates')}, 실패 {summary.total('failed')}, "
                  f"robots 건너뜀 {summary.total('skipped_robots')}, 봇 차단 감지 {summary.total('blocked')}곳, "
                  f"쿨다운 건너뜀 {summary.total('cooldown')}곳")
            print(f"DB 누적: 기사 {tot['articles']}건, 사이트 {tot['sites']}곳 ({args.db})")
            if args.report:
                write_report(summary, storage, args.report)
                print(f"보고서: {args.report}")

    if not args.every:
        run_once()
        return 0
    print(f"{args.every}분마다 반복합니다. 멈추려면 Ctrl+C.", flush=True)
    try:
        while True:
            print(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} 수집 시작 ===", flush=True)
            run_once()
            print(f"다음 실행: {time.strftime('%H:%M:%S', time.localtime(time.time() + args.every * 60))}", flush=True)
            time.sleep(args.every * 60)
    except KeyboardInterrupt:
        print("\n중단했습니다.")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    out = args.out or os.path.join("data", "export", f"articles_{time.strftime('%Y%m%d_%H%M')}.{args.format}")
    with Storage(args.db) as storage:
        n = storage.export(out, args.format, site=args.site, since=args.since, until=args.until,
                           ok_only=not args.all, limit=args.limit)
    print(f"{n}건 → {out}")
    return 0 if n else 1


def cmd_stats(args: argparse.Namespace) -> int:
    with Storage(args.db) as storage:
        t = storage.totals()
        print(f"DB: {args.db}")
        print(f"기사 {t['articles']}건 (본문 성공 {t['ok']}), 사이트 {t['sites']}곳, 날짜 {t['date_min']} ~ {t['date_max']}, 실패 기록 {t['failures']}건")
        run = storage.last_run()
        if run:
            print(f"마지막 실행 #{run['id']}: {run['started_at']} ~ {run['finished_at']}, 사이트 {run['sites']}, "
                  f"저장 {run['stored']}, 중복 {run['duplicates']}, 실패 {run['failed']}, robots 건너뜀 {run['skipped_robots']}")
        rows = storage.by_site(args.top)
        if rows:
            print(f"\n{'언론사':<16} {'도메인':<24} {'기사':>5} {'평균길이':>7}  최신")
            for r in rows:
                print(f"{(r['site_name'] or '')[:16]:<16} {r['site_domain'][:24]:<24} {r['n']:>5} {int(r['avg_len'] or 0):>7}  {r['latest'] or ''}")
        reasons = storage.failure_reasons(None, 8)
        if reasons:
            print("\n실패 이유 상위:")
            for err, n in reasons:
                print(f"  {n:>4}  {err}")
        blocked = storage.blocked_sites()
        if blocked:
            print("\n차단 쿨다운 중인 사이트 (이 시각까지 요청하지 않음):")
            for b in blocked:
                print(f"  {b['site_key']:<30} {b['blocked_until'][:16]}  {b['reason']}")
    return 0


# ---------------------------------------------------------------- common crawl
def cmd_cc_indices(args: argparse.Namespace) -> int:
    indices = cc_list_indices()
    print(f"사용 가능한 Common Crawl 인덱스 ({len(indices)}개):")
    for idx in indices[:args.top]:
        print(f"  {idx}")
    return 0


def cmd_cc_search(args: argparse.Namespace) -> int:
    records = cc_search(args.domain, index=args.index, limit=args.limit)
    if not records:
        print(f"'{args.domain}' 에 대한 HTML 결과가 없습니다 (인덱스: {args.index})")
        print("팁: 대형 도메인은 타임아웃될 수 있습니다. --index 를 바꾸거나 다른 시간대에 시도해 보세요.")
        print("    NYT 등 일부 매체는 robots.txt 로 CC 크롤링을 차단합니다.")
        return 1
    print(f"검색 결과: {len(records)}건 (인덱스: {args.index})")
    for i, r in enumerate(records, 1):
        print(f"  {i:3}. [{r.timestamp}] {r.url[:100]}")
    return 0


def cmd_cc_crawl(args: argparse.Namespace) -> int:
    articles = cc_crawl_domain(
        args.domain,
        index=args.index,
        limit=args.limit,
        url_filter=args.url_filter,
        gap=args.gap,
        article_pattern=args.pattern,
    )
    if not articles:
        print("수집된 기사가 없습니다.")
        return 1

    ok_articles = [a for a in articles if a.ok]

    if args.save_db:
        with Storage(args.db) as storage:
            saved = 0
            for a in ok_articles:
                try:
                    from crawler.storage import text_hash as _th, _now
                    h = _th(a.text) if a.text else None
                    with storage.lock:
                        if storage.conn.execute("SELECT 1 FROM articles WHERE url=?", (a.url,)).fetchone():
                            continue
                        storage.conn.execute(
                            """INSERT INTO articles (url, final_url, site_domain, site_name, title, text, text_hash,
                               date, author, extractor, text_length, ok, http_status, source, fetched_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (a.url, a.url, args.domain, args.domain, a.title, a.text, h,
                             a.date, a.author, "trafilatura/cc", a.text_length, 1, 200, "commoncrawl", _now()))
                        storage.conn.commit()
                    saved += 1
                except Exception:
                    pass
            print(f"DB 저장: {saved}건 → {args.db}")

    if args.out:
        out_path = args.out
    else:
        os.makedirs(os.path.join("data", "cc"), exist_ok=True)
        out_path = os.path.join("data", "cc", f"{args.domain.replace('.', '_')}_{args.index}.jsonl")

    with open(out_path, "w", encoding="utf-8") as f:
        for a in articles:
            f.write(json.dumps(a.to_dict(), ensure_ascii=False) + "\n")
    print(f"결과 저장: {out_path} ({len(articles)}건)")
    return 0


# ---------------------------------------------------------------- guardian
def cmd_guardian(args: argparse.Namespace) -> int:
    from crawler.guardian import GuardianClient
    gc = GuardianClient(api_key=args.api_key)
    if not gc.has_key and args.guardian_cmd != "sections":
        print("Guardian API 키가 필요합니다.")
        print("  발급: https://bonobo.capi.gutools.co.uk/register/developer")
        print("  사용: python main.py guardian --api-key YOUR_KEY search korea")
        print("  또는: set GUARDIAN_API_KEY=YOUR_KEY (환경변수)")
        gc.close()
        return 1
    with gc:
        if args.guardian_cmd == "sections":
            sections = gc.sections()
            print(f"Guardian 섹션 ({len(sections)}개):")
            for s in sections:
                print(f"  {s['id']:<30} {s['name']}")
            return 0

        if args.guardian_cmd == "search":
            articles, total, pages = gc.search(
                args.query, section=args.section,
                from_date=args.from_date, to_date=args.to_date,
                order_by=args.order_by, page=args.page, page_size=args.page_size,
            )
            print(f"검색 결과: {total}건 (페이지 {args.page}/{pages})")
            for i, a in enumerate(articles, 1):
                ok = "O" if a.ok else "X"
                print(f"  {i:3}. [{ok}] {a.date or '날짜없음'} | {a.title[:60]}")
                print(f"       {a.url}")
            return 0

        if args.guardian_cmd == "crawl":
            articles = gc.search_all(
                args.query, section=args.section,
                from_date=args.from_date, to_date=args.to_date,
                order_by=args.order_by, limit=args.limit,
            )
            ok_articles = [a for a in articles if a.ok]
            print(f"수집: {len(articles)}건 (본문 성공 {len(ok_articles)}건)")
            if not articles:
                return 1

            if args.save_db:
                from crawler.extract import Article
                with Storage(args.db) as storage:
                    saved = dup = 0
                    for a in ok_articles:
                        art = Article(
                            url=a.url, title=a.title, text=a.text,
                            date=a.date, author=a.author,
                            publisher="The Guardian", extractor="guardian-api",
                            text_length=a.text_length, ok=True,
                        )
                        outcome = storage.add_article(art, site_domain="theguardian.com",
                                                       site_name="The Guardian", source="guardian-api")
                        if outcome == "stored":
                            saved += 1
                        elif outcome == "duplicate":
                            dup += 1
                    print(f"DB 저장: {saved}건, 중복 {dup}건 → {args.db}")

            if args.out:
                import json as _json
                os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
                with open(args.out, "w", encoding="utf-8") as f:
                    for a in articles:
                        f.write(_json.dumps(a.to_dict(), ensure_ascii=False) + "\n")
                print(f"JSONL 저장: {args.out}")

            if not args.save_db and not args.out:
                for i, a in enumerate(ok_articles[:5], 1):
                    print(f"\n--- {i}. {a.title} ---")
                    print(f"날짜: {a.date}  기자: {a.author}  섹션: {a.section}")
                    preview = a.text[:300] + " …" if len(a.text) > 300 else a.text
                    print(preview)
                if len(ok_articles) > 5:
                    print(f"\n… 외 {len(ok_articles) - 5}건 (--out 또는 --save-db 로 저장)")
            return 0

    return 1


# ---------------------------------------------------------------- nyt
def cmd_nyt(args: argparse.Namespace) -> int:
    from crawler.nyt import NYTClient
    nc = NYTClient(api_key=args.api_key)
    if not nc.has_key:
        print("NYT API 키가 필요합니다.")
        print("  발급: https://developer.nytimes.com/accounts/create")
        print("  사용: python main.py nyt --api-key YOUR_KEY search korea")
        print("  또는: .env 파일에 NYT_API_KEY=YOUR_KEY 추가")
        nc.close()
        return 1
    with nc:
        if args.nyt_cmd == "top":
            articles = nc.top_stories(args.section)
            print(f"NYT Top Stories [{args.section}] ({len(articles)}건):")
            for i, a in enumerate(articles, 1):
                print(f"  {i:3}. {a.date or '날짜없음'} | {a.title[:70]}")
                if a.abstract:
                    print(f"       {a.abstract[:80]}")
            return 0

        if args.nyt_cmd == "search":
            fq = f'section_name:("{args.section}")' if args.section else None
            articles, total = nc.search(
                args.query, fq=fq,
                begin_date=args.begin_date, end_date=args.end_date,
                sort=args.sort, page=args.page,
            )
            print(f"검색 결과: {total}건 (페이지 {args.page + 1}, 10건/페이지)")
            for i, a in enumerate(articles, 1):
                print(f"  {i:3}. {a.date or '날짜없음'} | {a.title[:70]}")
                print(f"       {a.url}")
            return 0

        if args.nyt_cmd == "crawl":
            fq = f'section_name:("{args.section}")' if args.section else None
            print(f"수집 시작 (분당 5건 제한, 느릴 수 있음)…")
            articles = nc.search_all(
                args.query, fq=fq,
                begin_date=args.begin_date, end_date=args.end_date,
                sort=args.sort, limit=args.limit,
            )
            ok_articles = [a for a in articles if a.ok]
            print(f"수집: {len(articles)}건 (유효 {len(ok_articles)}건)")
            if not articles:
                return 1

            if args.save_db:
                from crawler.extract import Article
                with Storage(args.db) as storage:
                    saved = dup = 0
                    for a in ok_articles:
                        text = f"{a.abstract}\n\n{a.lead_paragraph}".strip() if a.lead_paragraph and a.lead_paragraph != a.abstract else a.abstract
                        art = Article(
                            url=a.url, title=a.title, text=text,
                            date=a.date, author=a.author,
                            publisher="The New York Times", extractor="nyt-api",
                            text_length=len(text), ok=len(text) >= 20,
                        )
                        outcome = storage.add_article(art, site_domain="nytimes.com",
                                                       site_name="NYT", source="nyt-api")
                        if outcome == "stored":
                            saved += 1
                        elif outcome == "duplicate":
                            dup += 1
                    print(f"DB 저장: {saved}건, 중복 {dup}건 → {args.db}")

            if args.out:
                import json as _json
                os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
                with open(args.out, "w", encoding="utf-8") as f:
                    for a in articles:
                        f.write(_json.dumps(a.to_dict(), ensure_ascii=False) + "\n")
                print(f"JSONL 저장: {args.out}")

            if not args.save_db and not args.out:
                for i, a in enumerate(ok_articles[:5], 1):
                    print(f"\n--- {i}. {a.title} ---")
                    print(f"날짜: {a.date}  기자: {a.author}  섹션: {a.section}")
                    print(a.abstract)
                if len(ok_articles) > 5:
                    print(f"\n… 외 {len(ok_articles) - 5}건 (--out 또는 --save-db 로 저장)")
            return 0

    return 1


def cmd_unblock(args: argparse.Namespace) -> int:
    with Storage(args.db) as storage:
        n = storage.clear_block(None if args.all else args.site)
    print(f"쿨다운 해제: {n}건")
    return 0


# ---------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="news-crawler", description="범용 뉴스 크롤러")
    sub = p.add_subparsers(dest="command", required=True)

    e = sub.add_parser("extract", help="URL 하나에서 기사 추출")
    e.add_argument("url")
    e.add_argument("--json", action="store_true", help="JSON으로 출력")
    e.add_argument("--timeout", type=float, default=15.0)
    e.add_argument("--preview", type=int, default=600, help="본문 미리보기 글자 수")
    e.add_argument("--js", action="store_true", help="Playwright 로 JS 렌더링 후 추출")
    e.set_defaults(func=cmd_extract)

    d = sub.add_parser("discover", help="사이트 자동 탐지 결과를 출력만 한다")
    d.add_argument("domain", help="예: hani.co.kr 또는 https://www.hani.co.kr/")
    d.add_argument("--name", help="언론사 이름(없으면 페이지에서 추정)")
    d.add_argument("--yaml", action="store_true", help="sites.yaml 블록 형식으로 출력")
    d.add_argument("--no-verify", action="store_true", help="기사 패턴 샘플 추출 검증 생략(빠름)")
    d.add_argument("--llm", action="store_true", help="자동 탐지가 비면 Claude 에 기사 URL 규칙을 제안받아 검증")
    d.add_argument("--llm-force", action="store_true", help="자동 탐지 결과가 있어도 Claude 제안을 추가로 받음")
    d.add_argument("--dry-run", action="store_true", help="--llm 과 함께: Claude 를 호출하지 않고 보낼 프롬프트만 출력")
    d.set_defaults(func=cmd_discover)

    a = sub.add_parser("add-site", help="자동 탐지 후 sites.yaml 에 추가")
    a.add_argument("domain")
    a.add_argument("--name")
    a.add_argument("--sites", default=DEFAULT_PATH, help=f"설정 파일 경로(기본 {DEFAULT_PATH})")
    a.add_argument("--no-verify", action="store_true")
    a.add_argument("--force", action="store_true", help="수집 경로를 못 찾아도 저장")
    a.add_argument("--llm", action="store_true", help="자동 탐지가 비면 Claude 에 기사 URL 규칙을 제안받아 검증")
    a.add_argument("--llm-force", action="store_true")
    a.set_defaults(func=cmd_add_site)

    ls = sub.add_parser("list-sites", help="등록된 사이트 목록")
    ls.add_argument("--sites", default=DEFAULT_PATH)
    ls.set_defaults(func=cmd_list_sites)

    c = sub.add_parser("crawl", help="등록된 사이트에서 새 기사를 받아 저장")
    c.add_argument("--sites", default=DEFAULT_PATH)
    c.add_argument("--db", default=DEFAULT_DB)
    c.add_argument("--only", help="쉼표로 구분한 도메인 또는 이름만")
    c.add_argument("--limit-per-site", type=int, default=30, help="사이트당 이번 실행에서 받을 새 기사 수")
    c.add_argument("--workers", type=int, default=6, help="동시에 처리할 사이트 수")
    c.add_argument("--gap", type=float, default=1.0, help="같은 사이트 안 요청 간격(초). robots Crawl-delay 가 더 크면 그 값")
    c.add_argument("--timeout", type=float, default=15.0)
    c.add_argument("--site-budget", type=float, default=240.0, help="사이트당 최대 시간(초)")
    c.add_argument("--strategies", default="rss,sitemap,pages", help="기사 주소 수집 순서")
    c.add_argument("--no-robots", action="store_true", help="robots.txt 를 무시 (권장하지 않음)")
    c.add_argument("--report", help="실행 보고서(markdown) 저장 경로")
    c.add_argument("--every", type=float, default=0, help="N분마다 반복 실행 (0 = 한 번만)")
    c.set_defaults(func=cmd_crawl)

    x = sub.add_parser("export", help="저장된 기사를 CSV/JSONL 로")
    x.add_argument("--db", default=DEFAULT_DB)
    x.add_argument("--format", choices=("csv", "jsonl"), default="csv")
    x.add_argument("--out", help="저장 경로(기본 data/export/articles_날짜.csv)")
    x.add_argument("--site", help="도메인 또는 언론사 이름")
    x.add_argument("--since", help="YYYY-MM-DD 이후")
    x.add_argument("--until", help="YYYY-MM-DD 까지")
    x.add_argument("--limit", type=int)
    x.add_argument("--all", action="store_true", help="본문 추출 실패 기록도 포함")
    x.set_defaults(func=cmd_export)

    s = sub.add_parser("stats", help="DB 현황")
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--top", type=int, default=25, help="사이트별 상위 N")
    s.set_defaults(func=cmd_stats)

    u = sub.add_parser("unblock", help="봇 차단 쿨다운 해제 (차단이 풀렸다고 확인했을 때만)")
    u.add_argument("site", nargs="?", help="사이트 키(홈 호스트). 예: nytimes.com")
    u.add_argument("--all", action="store_true")
    u.add_argument("--db", default=DEFAULT_DB)
    u.set_defaults(func=cmd_unblock)

    # ---- Common Crawl
    ci = sub.add_parser("cc-indices", help="사용 가능한 Common Crawl 인덱스 목록")
    ci.add_argument("--top", type=int, default=20, help="최근 N개만 출력")
    ci.set_defaults(func=cmd_cc_indices)

    cs = sub.add_parser("cc-search", help="Common Crawl 인덱스에서 도메인 검색")
    cs.add_argument("domain", help="예: nytimes.com")
    cs.add_argument("--index", default="CC-MAIN-2026-39", help="크롤 인덱스 이름")
    cs.add_argument("--limit", type=int, default=20, help="최대 결과 수")
    cs.set_defaults(func=cmd_cc_search)

    cc = sub.add_parser("cc-crawl", help="Common Crawl 에서 도메인 기사 수집")
    cc.add_argument("domain", help="예: nytimes.com")
    cc.add_argument("--index", default="CC-MAIN-2026-39", help="크롤 인덱스 이름")
    cc.add_argument("--limit", type=int, default=30, help="최대 수집 건수")
    cc.add_argument("--url-filter", help="URL 필터 (예: nytimes.com/2024/*)")
    cc.add_argument("--pattern", help="기사 URL 정규식 필터")
    cc.add_argument("--gap", type=float, default=0.3, help="WARC 요청 간격(초)")
    cc.add_argument("--out", help="결과 JSONL 저장 경로")
    cc.add_argument("--save-db", action="store_true", help="성공한 기사를 news.db 에도 저장")
    cc.add_argument("--db", default=DEFAULT_DB)
    cc.set_defaults(func=cmd_cc_crawl)

    # ---- NYT API
    n = sub.add_parser("nyt", help="New York Times API 로 기사 검색·수집")
    n.add_argument("--api-key", help="NYT API 키 (또는 NYT_API_KEY 환경변수)")
    n.add_argument("--db", default=DEFAULT_DB)
    nsub = n.add_subparsers(dest="nyt_cmd", required=True)

    ns = nsub.add_parser("search", help="기사 검색")
    ns.add_argument("query", nargs="?", help="검색어 (예: 'south korea')")
    ns.add_argument("--section", help="섹션 필터 (예: World, Business, Technology)")
    ns.add_argument("--begin-date", help="시작일 YYYY-MM-DD")
    ns.add_argument("--end-date", help="종료일 YYYY-MM-DD")
    ns.add_argument("--sort", default="newest", choices=("newest", "oldest", "relevance"))
    ns.add_argument("--page", type=int, default=0, help="페이지 번호 (0부터)")

    nc = nsub.add_parser("crawl", help="기사 수집 (여러 페이지, 느림)")
    nc.add_argument("query", nargs="?", help="검색어")
    nc.add_argument("--section", help="섹션 필터")
    nc.add_argument("--begin-date", help="시작일 YYYY-MM-DD")
    nc.add_argument("--end-date", help="종료일 YYYY-MM-DD")
    nc.add_argument("--sort", default="newest", choices=("newest", "oldest", "relevance"))
    nc.add_argument("--limit", type=int, default=50, help="최대 수집 건수 (10건당 12초)")
    nc.add_argument("--save-db", action="store_true", help="DB(news.db)에도 저장")
    nc.add_argument("--out", help="JSONL 저장 경로")

    nt = nsub.add_parser("top", help="Top Stories (편집자 선정 주요 기사)")
    nt.add_argument("--section", default="home", help="섹션 (home, world, business, technology, …)")
    n.set_defaults(func=cmd_nyt)

    # ---- Guardian API
    g = sub.add_parser("guardian", help="The Guardian API 로 기사 검색·수집")
    g.add_argument("--api-key", help="Guardian API 키 (또는 GUARDIAN_API_KEY 환경변수)")
    g.add_argument("--db", default=DEFAULT_DB)
    gsub = g.add_subparsers(dest="guardian_cmd", required=True)

    gs = gsub.add_parser("search", help="기사 검색")
    gs.add_argument("query", nargs="?", help="검색어 (예: 'korea climate')")
    gs.add_argument("--section", help="섹션 필터 (예: world, technology, sport)")
    gs.add_argument("--from-date", help="시작일 YYYY-MM-DD")
    gs.add_argument("--to-date", help="종료일 YYYY-MM-DD")
    gs.add_argument("--order-by", default="newest", choices=("newest", "oldest", "relevance"))
    gs.add_argument("--page", type=int, default=1)
    gs.add_argument("--page-size", type=int, default=20)

    gc = gsub.add_parser("crawl", help="기사 수집 (여러 페이지)")
    gc.add_argument("query", nargs="?", help="검색어")
    gc.add_argument("--section", help="섹션 필터")
    gc.add_argument("--from-date", help="시작일 YYYY-MM-DD")
    gc.add_argument("--to-date", help="종료일 YYYY-MM-DD")
    gc.add_argument("--order-by", default="newest", choices=("newest", "oldest", "relevance"))
    gc.add_argument("--limit", type=int, default=100, help="최대 수집 건수")
    gc.add_argument("--save-db", action="store_true", help="DB(news.db)에도 저장")
    gc.add_argument("--out", help="JSONL 저장 경로")

    gsc = gsub.add_parser("sections", help="사용 가능한 섹션 목록")
    g.set_defaults(func=cmd_guardian)

    return p


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
