import re
from collections import defaultdict

from crawler.discover import (build_regex, clean_site_name, matches_any, merge_groups, normalize_url,
                              registrable_domain, url_tokens)


def test_registrable_domain():
    assert registrable_domain("www.hani.co.kr") == "hani.co.kr"
    assert registrable_domain("news.kbs.co.kr") == "kbs.co.kr"
    assert registrable_domain("kr.people.com.cn") == "people.com.cn"
    assert registrable_domain("byline.network") == "byline.network"


def test_normalize_url_strips_tracking_and_sorts():
    u = "https://WWW.Hani.co.kr/arti/1.html?utm_source=x&b=2&a=1#top"
    assert normalize_url(u) == "https://www.hani.co.kr/arti/1.html?a=1&b=2"
    assert normalize_url("javascript:void(0)") == ""
    assert normalize_url("mailto:a@b.c") == ""


def _merged(urls):
    groups = defaultdict(list)
    for u in urls:
        nu = normalize_url(u)
        groups[url_tokens(nu)].append(nu)
    return [(build_regex(m["host"], m["path"], m["query"], m["trailing"]), m["urls"]) for m in merge_groups(groups)]


def test_category_paths_merge_into_one_pattern():
    urls = ["https://www.hani.co.kr/arti/opinion/column/1281419.html", "https://www.hani.co.kr/arti/sports/baseball/1281422.html",
            "https://www.hani.co.kr/arti/science/future/1281415.html"]
    (rx, members), = _merged(urls)
    assert rx == r"^https?://(?:www\.)?hani\.co\.kr/arti/[^/]+/[^/]+/\d+\.html(?:[?#&].*)?$"
    assert all(re.match(rx, u) for u in members)
    assert matches_any("https://www.hani.co.kr/arti/politics/assembly/1281300.html?utm_source=x", [rx])
    assert not matches_any("https://www.hani.co.kr/arti/politics/", [rx])


def test_date_path_with_random_id_merges():
    # 사이트맵처럼 같은 섹션의 기사가 먼저 두 개씩 오면 ID 자리 → 섹션 자리 순으로 넓어져 하나로 합쳐진다
    urls = []
    for i, sec in enumerate(["economy/science", "economy/science", "politics/assembly", "politics/assembly",
                             "national/court", "english/industry-en"], 1):
        urls.append(f"https://www.chosun.com/{sec}/2026/10/0{i}/ABCDEFGHIJKLMNOPQRSTUVWX{i}Z/")
    (rx, members), = _merged(urls)
    assert rx == r"^https?://(?:www\.)?chosun\.com/[^/]+/[^/]+/\d+/\d+/\d+/[^/]+/(?:[?#&].*)?$"
    assert len(members) == 6 and all(re.match(rx, u) for u in members)


def test_query_id_pattern_keeps_literal_params():
    urls = ["http://www.cctoday.co.kr/news/articleView.html?idxno=2237905&sc_section_code=S1N1",
            "http://www.cctoday.co.kr/news/articleView.html?idxno=2237999&sc_section_code=S1N1"]
    (rx, members), = _merged(urls)
    assert r"idxno=\d+" in rx and "sc_section_code=S1N1" in rx
    assert all(re.match(rx, u) for u in members)


def test_clean_site_name():
    assert clean_site_name("미래를 보는 창 - 전자신문") == "전자신문"
    assert clean_site_name("연합뉴스 | 한국 대표 뉴스통신") == "연합뉴스"
    assert clean_site_name("  한겨레 ") == "한겨레"
    assert clean_site_name("") is None
