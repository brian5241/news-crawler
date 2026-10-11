import csv

from crawler.extract import Article
from crawler.storage import Storage, text_hash


def _art(url, text, **kw):
    base = dict(url=url, final_url=url, title="제목", text=text, text_length=len(text), ok=True, http_status=200,
                extractor="trafilatura", date="2026-10-09 10:00")
    base.update(kw)
    return Article(**base)


def test_store_exists_duplicate_and_export(tmp_path):
    db = tmp_path / "t.db"
    with Storage(str(db)) as st:
        body = "본문입니다. " * 20
        assert st.add_article(_art("https://x.test/a/1", body), site_domain="x.test", site_name="X", source="rss") == "stored"
        assert st.add_article(_art("https://x.test/a/1", body), site_domain="x.test", site_name="X", source="rss") == "exists"
        assert st.add_article(_art("https://x.test/a/2", body), site_domain="x.test", site_name="X", source="rss") == "duplicate"
        assert st.add_article(_art("https://y.test/a/2", body), site_domain="y.test", site_name="Y", source="rss") == "stored"  # 다른 사이트는 중복 아님
        assert st.add_article(_art("https://x.test/a/3", "다른 본문. " * 30, date="2026-10-08"), site_domain="x.test", site_name="X", source="sitemap") == "stored"
        assert st.known_urls(["https://x.test/a/1", "https://x.test/a/9"]) == {"https://x.test/a/1"}
        t = st.totals()
        assert t["articles"] == 3 and t["sites"] == 2 and t["date_max"] == "2026-10-09 10:00"
        out = tmp_path / "e.csv"
        assert st.export(str(out), "csv", since="2026-10-09") == 2
        rows = list(csv.DictReader(open(out, encoding="utf-8-sig")))
        assert {r["site_domain"] for r in rows} == {"x.test", "y.test"}
        assert st.export(str(tmp_path / "e.jsonl"), "jsonl", site="X") == 2


def test_runs_failures_and_blocks(tmp_path):
    with Storage(str(tmp_path / "t.db")) as st:
        rid = st.start_run(1)
        st.add_failure(rid, "https://x.test/bad", "x.test", 404, "HTTP 404")
        st.finish_run(rid, found=1, new=1, stored=0, duplicates=0, failed=1, skipped_robots=0)
        assert st.last_run()["failed"] == 1 and st.failure_reasons(rid) == [("HTTP 404", 1)]
        until = st.set_block("x.test", 24, "bot challenge")
        assert st.get_block("x.test")["reason"] == "bot challenge" and until > "2026"
        assert [b["site_key"] for b in st.blocked_sites()] == ["x.test"]
        assert st.clear_block("x.test") == 1 and st.get_block("x.test") is None


def test_text_hash_ignores_whitespace_and_case():
    assert text_hash("A  b\nC") == text_hash("a b c")
