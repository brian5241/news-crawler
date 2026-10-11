"""수집 파이프라인.

사이트 블록(sites.yaml 한 항목)마다:
  1. 기사 주소 수집 (sources.Collector: RSS → 뉴스 사이트맵 → 목록 페이지)
  2. 이미 저장된 주소는 제외 (재실행하면 새 기사만 받는다)
  3. robots.txt 확인 (허용 안 된 주소는 건너뛰고 셈), Crawl-delay 와 설정의 crawl_delay 중 큰 값을 요청 간격으로
  4. 받기 → 추출 (extract) → 저장 (storage). 같은 사이트 안에서 본문이 같은 기사는 중복으로 세고 저장하지 않는다
사이트끼리는 스레드로 동시에, 한 사이트 안에서는 순서대로 간격을 두고 요청한다.

안 막히기 위한 장치
  - 네트워크 오류가 연달아 나는 사이트는 그 자리에서 포기한다 (죽은 주소를 계속 두드리지 않도록)
  - 403·429·봇 검사 페이지가 한 번이라도 오면 그 사이트에 즉시 요청을 끊고, DB(site_state)에 쿨다운을 적어
    다음 실행에서도 그 시간이 지날 때까지 건드리지 않는다 (기본 24시간, 서버가 Retry-After 를 주면 그 이상)
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

from .config import site_key
from .extract import extract_from_html, looks_like_html, normalize_date
from .fetch import fetch, make_client
from .sources import Collector
from .storage import Storage

try:
    from .browser import BrowserFetcher
    _HAS_BROWSER = True
except Exception:
    _HAS_BROWSER = False

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))
ROBOTS_AGENT = "*"
MAX_DELAY = 10.0            # robots Crawl-delay 가 이보다 커도 이 값까지만 기다린다
BLOCK_COOLDOWN_HOURS = 24.0  # 봇 차단 응답을 받은 사이트를 다시 건드리기까지


@dataclass
class SiteResult:
    domain: str
    name: str
    found: int = 0            # 수집한 기사 주소 수
    new: int = 0              # 그중 아직 저장되지 않은 수(이번에 시도한 수)
    stored: int = 0
    duplicates: int = 0
    failed: int = 0
    skipped_robots: int = 0
    blocked: int = 0          # 이번 실행에서 봇 차단 응답을 받아 중단했으면 1
    cooldown: int = 0         # 이전 차단의 쿨다운 중이라 아예 요청하지 않았으면 1
    elapsed: float = 0.0
    note: str = ""


@dataclass
class RunSummary:
    run_id: int
    started_at: str
    finished_at: str = ""
    sites: list[SiteResult] = field(default_factory=list)

    def total(self, key: str) -> int:
        return sum(getattr(s, key) for s in self.sites)


def load_robots(home: str, client, timeout: float) -> RobotFileParser:
    rp = RobotFileParser()
    res = fetch(urljoin(home, "/robots.txt"), client=client, timeout=timeout, retries=1)
    if res.ok and res.text and not looks_like_html(res.text):
        rp.parse(res.text.splitlines())
    else:
        rp.parse([])   # robots.txt 가 없거나 HTML 이면 전부 허용
    return rp


class Crawler:
    def __init__(self, storage: Storage, *, limit_per_site: int = 30, gap: float = 1.0, timeout: float = 15.0,
                 respect_robots: bool = True, site_budget: float = 240.0,
                 strategies: tuple[str, ...] = ("rss", "sitemap", "pages"),
                 block_cooldown_hours: float = BLOCK_COOLDOWN_HOURS):
        self.storage = storage
        self.limit = limit_per_site
        self.gap = gap
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.site_budget = site_budget
        self.strategies = strategies
        self.block_cooldown_hours = block_cooldown_hours
        self.run_id: Optional[int] = None

    def crawl_site(self, site: dict) -> SiteResult:
        t0 = time.monotonic()
        r = SiteResult(domain=site_key(site), name=site.get("name", ""))   # DB의 site_domain = 홈 호스트 키
        try:
            self._crawl_site(site, r, t0)
        except Exception as exc:  # 한 사이트의 사고가 전체 실행을 멈추면 안 된다
            log.warning("crawl failed for %s: %s", r.domain, exc)
            r.note = f"{type(exc).__name__}: {exc}"[:200]
        r.elapsed = round(time.monotonic() - t0, 1)
        return r

    def _mark_blocked(self, r: SiteResult, col: Collector) -> None:
        hours = self.block_cooldown_hours
        if col.retry_after:
            hours = max(hours, col.retry_after / 3600.0)
        until = self.storage.set_block(r.domain, hours, col.blocked or "blocked")
        r.blocked = 1
        r.note = f"봇 차단 응답({col.blocked}) → {until[:16]}까지 요청 안 함"

    def _crawl_site(self, site: dict, r: SiteResult, t0: float) -> None:
        block = self.storage.get_block(r.domain)
        if block:
            r.cooldown = 1
            r.note = f"차단 쿨다운 중({block['blocked_until'][:16]}까지, {block['reason']}) → 요청 안 함"
            return

        use_browser = site.get("js_render") and _HAS_BROWSER

        with make_client(self.timeout) as client:
            col = Collector(client=client, gap=self.gap, timeout=self.timeout)
            items = col.collect(site, limit=self.limit * 2 + 5, strategies=self.strategies)
            r.found = len(items)
            if col.blocked:
                self._mark_blocked(r, col)
                return
            if col.tripped:
                r.note = col.aborted or "네트워크 오류 연속으로 중단"
                return
            known = self.storage.known_urls([i["url"] for i in items])
            todo = [i for i in items if i["url"] not in known][: self.limit]
            r.new = len(todo)
            if not todo:
                r.note = "새 기사 없음" if items else "기사 주소를 못 모음"
                return

            rules = load_robots(site["home"], client, self.timeout) if self.respect_robots else None
            delay = self.gap
            for d in (site.get("crawl_delay"), rules.crawl_delay(ROBOTS_AGENT) if rules else None):
                if d:
                    delay = max(delay, float(d))
            col.gap = min(delay, MAX_DELAY)

            bf = None
            if use_browser:
                bf = BrowserFetcher(timeout=30.0, gap=col.gap)

            try:
                for it in todo:
                    if time.monotonic() - t0 > self.site_budget:
                        r.note = f"시간 예산 {self.site_budget:.0f}초 초과"
                        break
                    if rules is not None and not rules.can_fetch(ROBOTS_AGENT, it["url"]):
                        r.skipped_robots += 1
                        continue
                    if bf:
                        res = bf.fetch(it["url"])
                    else:
                        res = col.get(it["url"])
                    if col.blocked:
                        self._mark_blocked(r, col)
                        break
                    if col.tripped and not bf:
                        r.note = col.aborted or "네트워크 오류 연속으로 중단"
                        break
                    if not res.ok or not looks_like_html(res.text):
                        r.failed += 1
                        self.storage.add_failure(self.run_id, it["url"], r.domain, res.status, res.error or "non-HTML content")
                        continue
                    art = extract_from_html(res.text, it["url"], final_url=res.final_url,
                                            http_status=res.status, encoding=res.encoding)
                    if not art.ok:
                        r.failed += 1
                        self.storage.add_failure(self.run_id, it["url"], r.domain, res.status,
                                                 f"extract failed ({art.extractor}, {art.text_length}자)")
                        self.storage.mark_seen(it["url"], r.domain, "extract-failed")
                        continue
                    if not art.date and it.get("published"):
                        art.date = normalize_date(it["published"])
                    if not art.title and it.get("title"):
                        art.title = it["title"]
                    outcome = self.storage.add_article(art, site_domain=r.domain, site_name=r.name, source=it["source"])
                    if outcome == "stored":
                        r.stored += 1
                    elif outcome == "duplicate":
                        r.duplicates += 1
                        self.storage.mark_seen(it["url"], r.domain, "duplicate")
            finally:
                if bf:
                    bf.close()

    def crawl(self, sites: list[dict], *, workers: int = 6,
              progress: Optional[Callable[[int, int, SiteResult], None]] = None) -> RunSummary:
        self.run_id = self.storage.start_run(len(sites))
        summary = RunSummary(run_id=self.run_id, started_at=datetime.now(KST).isoformat(timespec="seconds"))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(self.crawl_site, s): s for s in sites}
            for i, fu in enumerate(as_completed(futs), 1):
                r = fu.result()
                summary.sites.append(r)
                self.storage.add_run_site(self.run_id, r)
                if progress:
                    progress(i, len(sites), r)
        summary.finished_at = datetime.now(KST).isoformat(timespec="seconds")
        notes = f"blocked={summary.total('blocked')} cooldown={summary.total('cooldown')}"
        self.storage.finish_run(self.run_id, found=summary.total("found"), new=summary.total("new"),
                                stored=summary.total("stored"), duplicates=summary.total("duplicates"),
                                failed=summary.total("failed"), skipped_robots=summary.total("skipped_robots"),
                                notes=notes)
        return summary


def write_report(summary: RunSummary, storage: Storage, path: str) -> None:
    """실행 결과를 마크다운으로."""
    sites = sorted(summary.sites, key=lambda s: (-s.stored, s.name))
    tot = storage.totals()
    L = [f"# 수집 실행 보고 (run {summary.run_id}, {summary.started_at[:16]} ~ {summary.finished_at[11:16]})", "",
         "| 항목 | 값 |", "|---|---|",
         f"| 대상 사이트 | {len(sites)} |",
         f"| 수집한 기사 주소 | {summary.total('found')} |",
         f"| 새 주소(시도) | {summary.total('new')} |",
         f"| 저장 | {summary.total('stored')} |",
         f"| 본문 중복(같은 사이트) | {summary.total('duplicates')} |",
         f"| 실패(받기·추출) | {summary.total('failed')} |",
         f"| robots.txt 로 건너뜀 | {summary.total('skipped_robots')} |",
         f"| 봇 차단 응답으로 중단한 사이트 | {summary.total('blocked')} |",
         f"| 차단 쿨다운 중이라 건너뛴 사이트 | {summary.total('cooldown')} |",
         f"| 저장 후 DB 누적 | 기사 {tot['articles']}건, 사이트 {tot['sites']}곳, 날짜 {tot['date_min']} ~ {tot['date_max']} |", "",
         "## 실패 이유 상위", "", "| 이유 | 건수 |", "|---|---|"]
    for err, n in storage.failure_reasons(summary.run_id, 12):
        L.append(f"| {err} | {n} |")
    L += ["", "## 사이트별", "", "| 언론사 | 주소 수집 | 새 주소 | 저장 | 중복 | 실패 | robots 건너뜀 | 초 | 메모 |",
          "|---|---|---|---|---|---|---|---|---|"]
    for s in sites:
        L.append(f"| {s.name} | {s.found} | {s.new} | {s.stored} | {s.duplicates} | {s.failed} | {s.skipped_robots} | {s.elapsed} | {s.note} |")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
