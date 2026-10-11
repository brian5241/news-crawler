"""SQLite 저장소. 기사·실패·실행 기록을 담고 CSV/JSONL 로 내보낸다.

표
  articles  기사 1건 = 1행. url 이 유일키. text_hash 로 같은 사이트 안의 내용 중복을 막는다.
  failures  받기·추출에 실패한 주소와 이유 (run_id 로 실행과 연결)
  runs      crawl 실행 1회 = 1행 (합계)
  run_sites 실행 × 사이트별 집계
여러 스레드가 같은 Storage 를 쓰므로 모든 접근을 RLock 으로 감싼다.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

KST = timezone(timedelta(hours=9))
DEFAULT_DB = os.path.join("data", "news.db")

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS articles (
        id INTEGER PRIMARY KEY,
        url TEXT NOT NULL UNIQUE,
        final_url TEXT,
        site_domain TEXT NOT NULL,
        site_name TEXT,
        title TEXT,
        text TEXT,
        text_hash TEXT,
        date TEXT,
        author TEXT,
        publisher TEXT,
        description TEXT,
        language TEXT,
        extractor TEXT,
        text_length INTEGER,
        ok INTEGER,
        http_status INTEGER,
        source TEXT,
        fetched_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_articles_site_date ON articles(site_domain, date)",
    "CREATE INDEX IF NOT EXISTS idx_articles_hash ON articles(site_domain, text_hash)",
    """CREATE TABLE IF NOT EXISTS failures (
        id INTEGER PRIMARY KEY, run_id INTEGER, url TEXT, site_domain TEXT, status INTEGER, error TEXT, at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, sites INTEGER, found INTEGER, new INTEGER,
        stored INTEGER, duplicates INTEGER, failed INTEGER, skipped_robots INTEGER, notes TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS run_sites (
        run_id INTEGER, site_domain TEXT, site_name TEXT, found INTEGER, new INTEGER, stored INTEGER,
        duplicates INTEGER, failed INTEGER, skipped_robots INTEGER, elapsed REAL, note TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS site_state (
        site_key TEXT PRIMARY KEY, blocked_until TEXT, reason TEXT, updated_at TEXT
    )""",
    # 저장하지는 않았지만 이미 처리한 주소 (본문 중복, 추출 실패). 다음 실행에서 또 받지 않도록 기억한다
    """CREATE TABLE IF NOT EXISTS seen_urls (
        url TEXT PRIMARY KEY, site_domain TEXT, reason TEXT, at TEXT
    )""",
)
SEEN_FAILURE_TTL_HOURS = 72.0   # 추출 실패는 3일 뒤 다시 시도해 본다 (페이지가 고쳐질 수 있으므로). 중복은 영구
EXPORT_COLUMNS = ("id", "site_name", "site_domain", "title", "date", "author", "publisher", "url", "text_length",
                  "text", "description", "language", "extractor", "source", "fetched_at")


def text_hash(text: str) -> str:
    norm = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def _scalar(v: Any) -> Any:
    """SQLite 에 넣을 수 없는 값(list/dict)은 JSON 문자열로."""
    if v is None or isinstance(v, (str, int, float, bytes)):
        return v
    try:
        return json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(v)


class Storage:
    def __init__(self, path: str = DEFAULT_DB):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            for stmt in SCHEMA:
                self.conn.execute(stmt)
            self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.commit()
            self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 기사
    def known_urls(self, urls: Iterable[str]) -> set[str]:
        """저장된 기사 + 처리했지만 저장하지 않은 주소(중복은 영구, 실패는 TTL 안)."""
        urls = list(dict.fromkeys(urls))
        out: set[str] = set()
        cutoff = (datetime.now(KST) - timedelta(hours=SEEN_FAILURE_TTL_HOURS)).isoformat(timespec="seconds")
        with self.lock:
            for i in range(0, len(urls), 500):
                chunk = urls[i:i + 500]
                marks = ",".join("?" * len(chunk))
                out.update(r["url"] for r in self.conn.execute(f"SELECT url FROM articles WHERE url IN ({marks})", chunk))
                out.update(r["url"] for r in self.conn.execute(
                    f"SELECT url FROM seen_urls WHERE url IN ({marks}) AND (reason='duplicate' OR at > ?)", chunk + [cutoff]))
        return out

    def mark_seen(self, url: str, site_domain: str, reason: str) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO seen_urls (url, site_domain, reason, at) VALUES (?,?,?,?)",
                              (url, site_domain, reason[:60], _now()))
            self.conn.commit()

    def find_by_hash(self, h: str, site_domain: str) -> Optional[str]:
        with self.lock:
            row = self.conn.execute("SELECT url FROM articles WHERE site_domain=? AND text_hash=? LIMIT 1",
                                    (site_domain, h)).fetchone()
        return row["url"] if row else None

    def add_article(self, art, *, site_domain: str, site_name: str, source: str) -> str:
        """Article → 저장. 'stored' | 'exists'(같은 url) | 'duplicate'(같은 사이트에 같은 본문)"""
        h = text_hash(art.text) if art.text and art.text_length >= 80 else None
        with self.lock:
            if self.conn.execute("SELECT 1 FROM articles WHERE url=?", (art.url,)).fetchone():
                return "exists"
            if h and self.find_by_hash(h, site_domain):
                return "duplicate"
            self.conn.execute(
                """INSERT INTO articles (url, final_url, site_domain, site_name, title, text, text_hash, date, author,
                   publisher, description, language, extractor, text_length, ok, http_status, source, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                tuple(_scalar(v) for v in (
                    art.url, art.final_url, site_domain, site_name, art.title, art.text, h, art.date, art.author,
                    art.publisher, art.description, art.language, art.extractor, art.text_length, int(bool(art.ok)),
                    art.http_status, source, art.fetched_at or _now())))
            self.conn.commit()
        return "stored"

    def add_failure(self, run_id: Optional[int], url: str, site_domain: str, status: int, error: str) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO failures (run_id, url, site_domain, status, error, at) VALUES (?,?,?,?,?,?)",
                              (run_id, url, site_domain, status, (error or "")[:300], _now()))
            self.conn.commit()

    # ---- 실행 기록
    def start_run(self, sites: int) -> int:
        with self.lock:
            cur = self.conn.execute("INSERT INTO runs (started_at, sites) VALUES (?, ?)", (_now(), sites))
            self.conn.commit()
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, **totals: Any) -> None:
        with self.lock:
            self.conn.execute(
                """UPDATE runs SET finished_at=?, found=?, new=?, stored=?, duplicates=?, failed=?, skipped_robots=?, notes=?
                   WHERE id=?""",
                (_now(), totals.get("found", 0), totals.get("new", 0), totals.get("stored", 0),
                 totals.get("duplicates", 0), totals.get("failed", 0), totals.get("skipped_robots", 0),
                 totals.get("notes", ""), run_id))
            self.conn.commit()

    def add_run_site(self, run_id: int, r: Any) -> None:
        with self.lock:
            self.conn.execute(
                """INSERT INTO run_sites (run_id, site_domain, site_name, found, new, stored, duplicates, failed,
                   skipped_robots, elapsed, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, r.domain, r.name, r.found, r.new, r.stored, r.duplicates, r.failed, r.skipped_robots,
                 r.elapsed, r.note))
            self.conn.commit()

    # ---- 차단 쿨다운 (봇 차단·속도 제한을 받은 사이트는 한동안 건드리지 않는다)
    def get_block(self, site_key: str) -> Optional[dict[str, Any]]:
        with self.lock:
            row = self.conn.execute("SELECT * FROM site_state WHERE site_key=?", (site_key,)).fetchone()
        if not row or not row["blocked_until"]:
            return None
        if row["blocked_until"] <= _now():
            return None
        return dict(row)

    def set_block(self, site_key: str, hours: float, reason: str) -> str:
        until = (datetime.now(KST) + timedelta(hours=hours)).isoformat(timespec="seconds")
        with self.lock:
            self.conn.execute(
                "INSERT INTO site_state (site_key, blocked_until, reason, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(site_key) DO UPDATE SET blocked_until=excluded.blocked_until, reason=excluded.reason, "
                "updated_at=excluded.updated_at", (site_key, until, reason[:200], _now()))
            self.conn.commit()
        return until

    def clear_block(self, site_key: Optional[str] = None) -> int:
        with self.lock:
            cur = (self.conn.execute("DELETE FROM site_state") if site_key is None
                   else self.conn.execute("DELETE FROM site_state WHERE site_key=?", (site_key,)))
            self.conn.commit()
        return cur.rowcount

    def blocked_sites(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM site_state WHERE blocked_until > ? ORDER BY blocked_until",
                                     (_now(),)).fetchall()
        return [dict(r) for r in rows]

    # ---- 조회
    def totals(self) -> dict[str, Any]:
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) n, COUNT(DISTINCT site_domain) sites, MIN(date) d0, MAX(date) d1, "
                "SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) ok FROM articles").fetchone()
            fails = self.conn.execute("SELECT COUNT(*) n FROM failures").fetchone()["n"]
        return dict(articles=row["n"], ok=row["ok"] or 0, sites=row["sites"], date_min=row["d0"], date_max=row["d1"],
                    failures=fails)

    def by_site(self, limit: Optional[int] = None) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT site_domain, site_name, COUNT(*) n, MAX(date) latest, AVG(text_length) avg_len "
                "FROM articles GROUP BY site_domain ORDER BY n DESC" + (f" LIMIT {int(limit)}" if limit else "")
            ).fetchall()
        return [dict(r) for r in rows]

    def last_run(self) -> Optional[dict[str, Any]]:
        with self.lock:
            row = self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def run_sites(self, run_id: int) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM run_sites WHERE run_id=? ORDER BY stored DESC, site_name",
                                     (run_id,)).fetchall()
        return [dict(r) for r in rows]

    def failure_reasons(self, run_id: Optional[int] = None, limit: int = 10) -> list[tuple[str, int]]:
        q = "SELECT error, COUNT(*) n FROM failures" + (" WHERE run_id=?" if run_id else "") + \
            " GROUP BY error ORDER BY n DESC LIMIT ?"
        with self.lock:
            rows = self.conn.execute(q, ((run_id, limit) if run_id else (limit,))).fetchall()
        return [(r["error"], r["n"]) for r in rows]

    def iter_articles(self, *, site: Optional[str] = None, since: Optional[str] = None, until: Optional[str] = None,
                      ok_only: bool = True, limit: Optional[int] = None) -> Iterable[sqlite3.Row]:
        where, params = [], []
        if ok_only:
            where.append("ok=1")
        if site:
            where.append("(site_domain=? OR site_name=?)")
            params += [site, site]
        if since:
            where.append("date >= ?")
            params.append(since)
        if until:
            where.append("date <= ?")
            params.append(until + "~")   # 'YYYY-MM-DD~' 는 그 날짜의 모든 시각보다 크다
        q = "SELECT * FROM articles" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY date DESC, id DESC"
        if limit:
            q += f" LIMIT {int(limit)}"
        with self.lock:
            rows = self.conn.execute(q, params).fetchall()
        return rows

    # ---- 내보내기
    def export(self, out: str, fmt: str = "csv", **filters: Any) -> int:
        rows = list(self.iter_articles(**filters))
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        if fmt == "jsonl":
            with open(out, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps({k: r[k] for k in EXPORT_COLUMNS}, ensure_ascii=False) + "\n")
        else:
            with open(out, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(EXPORT_COLUMNS)
                for r in rows:
                    w.writerow([r[k] for k in EXPORT_COLUMNS])
        return len(rows)
