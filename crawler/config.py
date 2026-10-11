"""sites.yaml 읽기/쓰기.

형식:
sites:
  - name: 한겨레
    domain: hani.co.kr
    home: https://www.hani.co.kr/
    rss: [...]
    sitemaps: [...]
    news_sitemaps: [...]
    article_patterns: [...]     # 정규식. normalize_url() 한 URL에 re.match
    list_pages: [...]
    crawl_delay: null
    ...
사이트의 키는 홈 주소의 호스트(www. 제외)와 경로다. 예: hani.co.kr(한겨레), h21.hani.co.kr(한겨레21), en.yna.co.kr(연합뉴스 영문).
같은 키를 다시 add-site 하면 블록을 교체한다.
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

import yaml

DEFAULT_PATH = "sites.yaml"


def site_key(site: dict[str, Any]) -> str:
    """홈 주소 → 사이트 식별자. 'https://www.hani.co.kr/' → 'hani.co.kr', 'https://h21.hani.co.kr/' → 'h21.hani.co.kr',
    'https://kookbang.dema.mil.kr/newsWeb/main.do' → 'kookbang.dema.mil.kr/newsWeb/main.do'"""
    home = site.get("home") or ""
    p = urlsplit(home)
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = p.path.rstrip("/")
    return (host + path) if host else (site.get("domain") or "")
# 저장 순서(읽기 쉬운 순서). SiteConfig.to_dict() 키 중 여기 없는 것은 뒤에 붙는다.
_ORDER = ("name", "domain", "home", "rss", "sitemaps", "news_sitemaps", "article_patterns", "list_pages",
          "crawl_delay", "encoding", "discovered_at", "verified_samples", "notes", "error")


def load_sites(path: str = DEFAULT_PATH) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return list(data.get("sites") or [])


def save_sites(sites: list[dict[str, Any]], path: str = DEFAULT_PATH) -> None:
    ordered = [{k: s[k] for k in _ORDER if k in s} | {k: v for k, v in s.items() if k not in _ORDER} for s in sites]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump({"sites": ordered}, f, allow_unicode=True, sort_keys=False, width=200)
    os.replace(tmp, path)


def upsert_site(site: dict[str, Any], path: str = DEFAULT_PATH) -> bool:
    """같은 키(홈 호스트+경로)가 있으면 교체(True), 없으면 추가(False)."""
    sites = load_sites(path)
    key = site_key(site)
    for i, s in enumerate(sites):
        if site_key(s) == key:
            sites[i] = site
            save_sites(sites, path)
            return True
    sites.append(site)
    save_sites(sites, path)
    return False


def find_site(key_or_name: str, path: str = DEFAULT_PATH) -> dict[str, Any] | None:
    for s in load_sites(path):
        if site_key(s) == key_or_name or s.get("name") == key_or_name:
            return s
    return None


def select_sites(sites: list[dict[str, Any]], wanted: set[str]) -> list[dict[str, Any]]:
    """--only 용: 키, 이름, 또는 등록 도메인(여러 사이트가 공유할 수 있음)으로 고른다."""
    return [s for s in sites if site_key(s) in wanted or s.get("name") in wanted or s.get("domain") in wanted]
