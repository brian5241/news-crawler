import gzip
import re

import crawler.sources as sources
from crawler.fetch import FetchResult
from crawler.sources import Collector, _gunzip_if_needed

RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>t</title>
<item><title>첫 기사</title><link>https://www.site.co.kr/news/articleView.html?idxno=10001&amp;utm_source=rss</link><pubDate>Thu, 09 Oct 2026 01:00:00 +0900</pubDate></item>
<item><title>둘째 기사</title><link>https://www.site.co.kr/news/articleView.html?idxno=10002</link></item>
<item><title>중복</title><link>https://www.site.co.kr/news/articleView.html?idxno=10001</link></item>
<item><title>목록</title><link>https://www.site.co.kr/news/articleList.html?sc_section_code=S1N1</link></item>
</channel></rss>"""


def test_accept_rules():
    pats = [re.compile(r"^https?://(?:www\.)?site\.co\.kr/news/articleView\.html\?idxno=\d+(?:[?#&].*)?$")]
    acc = Collector._accept
    assert acc("https://www.site.co.kr/news/articleView.html?idxno=5", pats, "site.co.kr", False)  # 패턴 일치
    assert acc("https://other.com/news/articleView.html?idxno=5", pats, "site.co.kr", False) is None  # 다른 도메인
    assert acc("https://www.site.co.kr/photo/2026/10/09/123456", pats, "site.co.kr", True)   # RSS 는 숫자 4자리 이상이면 통과
    assert acc("https://www.site.co.kr/photo/2026/10/09/123456", pats, "site.co.kr", False) is None  # 패턴 있으면 사이트맵은 패턴만
    assert acc("https://www.site.co.kr/main2022/", [], "site.co.kr", False) is None   # 연도만 있는 안내 페이지
    assert acc("https://www.site.co.kr/view/12345", [], "site.co.kr", False)           # 패턴 없으면 5자리 이상
    assert acc("https://www.site.co.kr/news/articleList.html?idxno=12345", [], "site.co.kr", True) is None  # list 제외


def test_gunzip_handles_plain_and_gz():
    assert _gunzip_if_needed(b"<urlset/>") == b"<urlset/>"
    assert _gunzip_if_needed(gzip.compress(b"<urlset/>")) == b"<urlset/>"


def test_collect_from_rss_dedupes_and_parses_dates(monkeypatch):
    def fake_fetch(url, client=None, **kw):
        if url.endswith("rss.xml"):
            return FetchResult(url=url, final_url=url, status=200, content=RSS.encode(), text=RSS, content_type="application/xml")
        return FetchResult(url=url, final_url=url, status=404, error="HTTP 404")
    monkeypatch.setattr(sources, "fetch", fake_fetch)
    site = {"name": "사이트", "domain": "site.co.kr", "home": "https://www.site.co.kr/", "rss": ["https://www.site.co.kr/rss.xml"],
            "sitemaps": [], "news_sitemaps": [], "article_patterns": [], "list_pages": []}
    col = Collector(client=object(), gap=0)
    items = col.collect(site, limit=10, strategies=("rss",))
    urls = [i["url"] for i in items]
    assert urls == ["https://www.site.co.kr/news/articleView.html?idxno=10001", "https://www.site.co.kr/news/articleView.html?idxno=10002"]
    assert items[0]["published"] == "2026-10-09 01:00" and items[0]["title"] == "첫 기사" and items[0]["source"] == "rss"


def test_collector_trips_on_hard_block_and_soft_403_twice(monkeypatch):
    calls = {"n": 0}
    kind = {"v": "soft"}
    def fake_fetch(url, client=None, **kw):
        calls["n"] += 1
        if kind["v"] == "hard":
            return FetchResult(url=url, final_url=url, status=403, text="x", error="HTTP 403", blocked="bot challenge (datadome)", block_hard=True)
        return FetchResult(url=url, final_url=url, status=403, text="x", error="HTTP 403", blocked="HTTP 403 forbidden", block_hard=False)
    monkeypatch.setattr(sources, "fetch", fake_fetch)
    col = Collector(client=object(), gap=0)
    col.get("https://a.test/1"); assert not col.tripped
    col.get("https://a.test/2"); assert col.tripped and "×2" in col.blocked
    kind["v"] = "hard"; calls["n"] = 0
    col = Collector(client=object(), gap=0)
    col.get("https://a.test/1"); col.get("https://a.test/2")
    assert col.tripped and calls["n"] == 1   # 두 번째는 네트워크로 안 나감
