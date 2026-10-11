import crawler.pipeline as pipeline
import crawler.sources as sources
from crawler.fetch import FetchResult
from crawler.pipeline import Crawler
from crawler.storage import Storage

SITE = {"name": "테스트", "domain": "site.test", "home": "https://www.site.test/", "rss": ["https://www.site.test/rss.xml"],
        "sitemaps": [], "news_sitemaps": [], "article_patterns": [], "list_pages": []}
RSS = """<rss version="2.0"><channel><item><title>A</title><link>https://www.site.test/news/10001</link></item>
<item><title>B</title><link>https://www.site.test/news/10002</link></item></channel></rss>"""
BODY = "<p>" + "국정감사에서 인공지능 안전을 두고 여야 의원들이 공방을 벌였다. 정부는 예산을 늘리겠다고 답했다. " * 4 + "</p>"
ARTICLE = ("<html><head><title>기사 제목</title><meta property=\"article:published_time\" content=\"2026-10-09T10:00:00+09:00\">"
           "</head><body><article><h1>기사 제목</h1>" + BODY + "</article></body></html>")


def _serve(robots="User-agent: *\nDisallow: /secret/\n"):
    def fake_fetch(url, client=None, **kw):
        if url.endswith("robots.txt"):
            return FetchResult(url=url, final_url=url, status=200, text=robots, content=robots.encode())
        if url.endswith("rss.xml"):
            return FetchResult(url=url, final_url=url, status=200, text=RSS, content=RSS.encode())
        if "/news/" in url:
            return FetchResult(url=url, final_url=url, status=200, text=ARTICLE, content=ARTICLE.encode(), content_type="text/html")
        return FetchResult(url=url, final_url=url, status=404, error="HTTP 404")
    return fake_fetch


def test_crawl_site_stores_new_articles_and_skips_known(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "fetch", _serve())
    monkeypatch.setattr(pipeline, "fetch", _serve())
    with Storage(str(tmp_path / "t.db")) as st:
        cr = Crawler(st, limit_per_site=5, gap=0)
        r = cr.crawl_site(SITE)
        assert (r.found, r.new, r.stored, r.failed) == (2, 2, 1, 0)   # 두 기사 본문이 같아 하나는 중복
        assert r.duplicates == 1
        r2 = cr.crawl_site(SITE)
        assert r2.new == 0 and r2.note == "새 기사 없음"
        assert st.totals()["articles"] == 1


def test_robots_disallow_all_skips_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "fetch", _serve())
    monkeypatch.setattr(pipeline, "fetch", _serve(robots="User-agent: *\nDisallow: /\n"))
    with Storage(str(tmp_path / "t.db")) as st:
        r = Crawler(st, limit_per_site=5, gap=0).crawl_site(SITE)
        assert r.skipped_robots == 2 and r.stored == 0


def test_hard_block_sets_cooldown_and_next_run_skips(tmp_path, monkeypatch):
    calls = {"n": 0}
    def blocked_fetch(url, client=None, **kw):
        calls["n"] += 1
        return FetchResult(url=url, final_url=url, status=403, text="<title>Access Denied</title>", error="HTTP 403",
                           blocked="bot challenge (Access Denied)", block_hard=True)
    monkeypatch.setattr(sources, "fetch", blocked_fetch)
    monkeypatch.setattr(pipeline, "fetch", blocked_fetch)
    with Storage(str(tmp_path / "t.db")) as st:
        cr = Crawler(st, limit_per_site=5, gap=0)
        r = cr.crawl_site(SITE)
        assert r.blocked == 1 and calls["n"] == 1 and st.get_block("site.test") is not None
        calls["n"] = 0
        r2 = cr.crawl_site(SITE)
        assert r2.cooldown == 1 and calls["n"] == 0


def test_run_summary_and_report(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "fetch", _serve())
    monkeypatch.setattr(pipeline, "fetch", _serve())
    with Storage(str(tmp_path / "t.db")) as st:
        cr = Crawler(st, limit_per_site=5, gap=0)
        summary = cr.crawl([SITE], workers=1)
        assert summary.total("stored") == 1 and st.last_run()["stored"] == 1
        report = tmp_path / "r.md"
        pipeline.write_report(summary, st, str(report))
        assert "| 저장 | 1 |" in report.read_text(encoding="utf-8")
