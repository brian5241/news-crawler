"""LLM 보조 설정 생성 (7단계).

자동 탐지가 RSS·뉴스 사이트맵·기사 URL 패턴을 하나도 못 찾은 사이트에 대해, 홈페이지에서 모은 '증거'
(제목·메타·내부 링크 목록·<link> 피드 태그·robots 사이트맵 선언)를 Claude 에 보여 주고
기사 URL 규칙·기사 예시·목록 페이지·RSS 후보를 제안받는다.

제안은 그대로 믿지 않는다.
  - 정규식은 홈페이지의 실제 링크에 3개 이상 맞아야 하고, 그중 1건을 받아 기사로 추출돼야 채택 (자동 탐지와 같은 검증)
  - RSS 후보는 실제로 받아 항목이 있어야 채택
  - 목록 페이지는 같은 도메인만
자격 증명: ANTHROPIC_API_KEY 환경변수 또는 `ant auth login` 프로필 (SDK 가 알아서 찾는다). 없으면 --dry-run 으로 보낼 내용만 본다.
모델: claude-opus-5-5 (사이트당 호출 1회).
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from .discover import (Discoverer, SiteConfig, _EXCLUDE_RE, build_regex, merge_groups, normalize_url,
                       registrable_domain, url_tokens)
from .extract import _meta_map, _title_tag

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
MAX_LINKS_IN_PROMPT = 300
MIN_REGEX_MATCHES = 3

SYSTEM_PROMPT = """당신은 뉴스 사이트의 URL 구조를 분석하는 크롤러 설정 도우미다.
입력으로 한 뉴스 사이트의 홈페이지에서 모은 증거(제목, 메타 정보, 내부 링크 목록, 피드 태그, 사이트맵 선언)를 받는다.
할 일: 이 사이트에서 '개별 기사' 페이지의 URL 규칙을 찾고, 새 기사가 나열되는 목록 페이지와 RSS 주소 후보를 고른다.

규칙
- article_url_regexes: 파이썬 정규식. 정규화된 절대 URL(소문자 스킴·호스트, 추적 파라미터 제거)에 re.match 로 쓴다.
  숫자 ID 는 \\d+ 로, 카테고리처럼 바뀌는 경로 조각은 [^/]+ 로 일반화한다. 예: ^https?://(?:www\\.)?example\\.co\\.kr/news/articleView\\.html\\?idxno=\\d+(?:[?#&].*)?$
  목록·태그·검색·로그인·이미지·CSS 주소는 기사가 아니다.
- example_article_urls: 입력 링크 목록에 실제로 있는 기사 URL 만 3~8개. 지어내지 않는다.
- list_page_urls: 섹션·최신기사 목록 페이지. 입력 링크에 있는 것만.
- rss_urls: 입력에 피드 태그가 있으면 그 주소. 없으면 빈 목록. 추측으로 만들지 않는다.
- 확신이 없으면 빈 목록을 돌려준다. 틀린 규칙보다 빈 목록이 낫다."""


class Proposal(BaseModel):
    article_url_regexes: list[str] = Field(default_factory=list, description="기사 URL 정규식 후보")
    example_article_urls: list[str] = Field(default_factory=list, description="입력 링크 중 기사 URL 예시")
    list_page_urls: list[str] = Field(default_factory=list, description="새 기사가 나열되는 목록 페이지")
    rss_urls: list[str] = Field(default_factory=list, description="피드 주소 후보")
    reasoning: str = Field(default="", description="한두 문장 근거")


@dataclass
class Evidence:
    home: str
    title: str
    site_name: str
    links: list[tuple[str, str]] = field(default_factory=list)   # (정규화 URL, 앵커 텍스트)
    feed_tags: list[str] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)


class LLMUnavailable(RuntimeError):
    """자격 증명이 없거나 SDK 를 못 쓰는 경우."""


# ---------------------------------------------------------------- 증거 모으기

def build_evidence(home: str, tree, links: list[str], anchors: dict[str, str], sitemaps: list[str]) -> Evidence:
    meta = _meta_map(tree)
    site_dom = registrable_domain(urlsplit(home).netloc)
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for u in links:
        nu = normalize_url(u)
        if not nu or nu in seen or registrable_domain(urlsplit(nu).netloc) != site_dom:
            continue
        if re.search(r"\.(?:css|js|jpe?g|png|gif|svg|webp|ico|pdf)(?:$|\?)", nu, re.I):
            continue
        seen.add(nu)
        out.append((nu, (anchors.get(u) or "")[:40]))
    feed_tags: list[str] = []
    if tree is not None:
        for el in tree.xpath("//link[@href]"):
            t = (el.get("type") or "").lower()
            if "rss" in t or "atom" in t:
                feed_tags.append(el.get("href").strip())
    return Evidence(home=home, title=(_title_tag(tree) or "").strip()[:120],
                    site_name=(meta.get("og:site_name") or "")[:60], links=out, feed_tags=feed_tags, sitemaps=sitemaps)


def anchors_of(tree, base_links: list[str]) -> dict[str, str]:
    """링크 URL → 앵커 텍스트 (증거를 읽기 쉽게)."""
    out: dict[str, str] = {}
    if tree is None:
        return out
    from urllib.parse import urljoin
    for a in tree.xpath("//a[@href]"):
        try:
            u = urljoin(base_links[0] if base_links else "", (a.get("href") or "").strip())
        except ValueError:
            continue
        txt = re.sub(r"\s+", " ", a.text_content() or "").strip()
        if u and txt and u not in out:
            out[u] = txt
    return out


def evidence_to_prompt(ev: Evidence, max_links: int = MAX_LINKS_IN_PROMPT) -> str:
    # 같은 '꼴'의 링크가 수백 개면 템플릿별로 몇 개씩만 보여 준다 (토큰 절약, 다양성 확보)
    groups: dict = defaultdict(list)
    for nu, txt in ev.links:
        try:
            host, path, query, trailing = url_tokens(nu)
            key = (host, path, query, trailing)
        except Exception:
            key = ("?", nu, (), False)
        groups[key].append((nu, txt))
    picked: list[tuple[str, str, int]] = []
    for key, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        for nu, txt in items[:4]:
            picked.append((nu, txt, len(items)))
        if len(picked) >= max_links:
            break
    lines = [f"홈: {ev.home}", f"제목: {ev.title}", f"og:site_name: {ev.site_name}",
             f"피드 태그(link rel=alternate): {ev.feed_tags or '없음'}",
             f"robots 사이트맵 선언: {ev.sitemaps or '없음'}",
             f"내부 링크 {len(ev.links)}개 중 템플릿별 대표 {len(picked)}개 (형식: URL | 앵커 텍스트 | 같은 꼴 링크 수):"]
    lines += [f"{nu} | {txt} | {n}" for nu, txt, n in picked]
    return "\n".join(lines)


# ---------------------------------------------------------------- Claude 호출

def ask_claude(prompt: str, client=None, model: str = DEFAULT_MODEL) -> Optional[Proposal]:
    try:
        import anthropic
    except ImportError as exc:
        raise LLMUnavailable("anthropic SDK 가 설치되지 않았습니다: pip install anthropic") from exc
    if client is None:
        try:
            client = anthropic.Anthropic()
        except anthropic.AnthropicError as exc:   # 자격 증명 없음 등
            raise LLMUnavailable(f"Claude 자격 증명을 찾지 못했습니다 (ANTHROPIC_API_KEY 또는 ant auth login): {exc}") from exc
    try:
        response = client.messages.parse(
            model=model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_format=Proposal,
            output_config={"effort": "high"},
        )
    except anthropic.AuthenticationError as exc:
        raise LLMUnavailable(f"Claude 인증 실패: {exc.message}") from exc
    except anthropic.RateLimitError as exc:
        log.warning("Claude 속도 제한: %s", exc.message)
        return None
    except anthropic.APIStatusError as exc:
        log.warning("Claude API 오류 %s: %s", exc.status_code, exc.message)
        return None
    except anthropic.APIConnectionError as exc:
        log.warning("Claude 연결 오류: %s", exc)
        return None
    if response.stop_reason == "refusal":
        log.warning("Claude 가 응답을 거부했습니다: %s", getattr(response, "stop_details", None))
        return None
    if response.stop_reason == "max_tokens":
        log.warning("Claude 응답이 max_tokens 에서 잘렸습니다")
        return None
    return response.parsed_output


# ---------------------------------------------------------------- 제안 검증·적용

def apply_proposal(cfg: SiteConfig, proposal: Proposal, links: list[str], discoverer: Discoverer) -> list[str]:
    """제안을 실제 링크와 추출 검증으로 걸러 cfg 에 반영한다. 메모 목록을 돌려준다."""
    notes: list[str] = []
    site_dom = registrable_domain(urlsplit(cfg.home).netloc)
    norm_links = [nu for nu in (normalize_url(u) for u in links)
                  if nu and registrable_domain(urlsplit(nu).netloc) == site_dom]

    adopted = 0
    for rx in proposal.article_url_regexes[:6]:
        try:
            pat = re.compile(rx)
        except re.error as exc:
            notes.append(f"LLM 정규식 무효({exc}): {rx[:60]}")
            continue
        matched = [nu for nu in norm_links if pat.match(nu) and not _EXCLUDE_RE.search(nu)]
        if len(matched) < MIN_REGEX_MATCHES:
            notes.append(f"LLM 정규식이 실제 링크에 {len(matched)}개만 맞아 제외: {rx[:60]}")
            continue
        if discoverer.verify and not discoverer._verify_article(matched[0]):
            notes.append(f"LLM 정규식 샘플 추출 검증 실패: {matched[0]}")
            continue
        if rx not in cfg.article_patterns:
            cfg.article_patterns.append(rx)
            cfg.verified_samples.append(matched[0])
            adopted += 1
    if not adopted and proposal.example_article_urls:
        examples = [nu for nu in (normalize_url(u) for u in proposal.example_article_urls)
                    if nu and nu in set(norm_links)]   # 입력에 있던 링크만 (지어낸 주소 방지)
        groups: dict = defaultdict(list)
        for nu in examples:
            groups[url_tokens(nu)].append(nu)
        for m in merge_groups(groups)[:2]:
            if len(m["urls"]) < 2:
                continue
            rx = build_regex(m["host"], m["path"], m["query"], m["trailing"])
            if discoverer.verify and not discoverer._verify_article(m["urls"][0]):
                notes.append(f"LLM 예시에서 만든 패턴 검증 실패: {m['urls'][0]}")
                continue
            cfg.article_patterns.append(rx)
            cfg.verified_samples.append(m["urls"][0])
            adopted += 1
            break
    notes.append(f"LLM 제안 기사 패턴 채택 {adopted}개")

    feeds_added = 0
    for u in proposal.rss_urls[:5]:
        nu = normalize_url(u)
        if not nu or registrable_domain(urlsplit(nu).netloc) != site_dom or nu in cfg.rss:
            continue
        kind, entry_links, _ = discoverer._check_feed(nu)
        if kind == "feed":
            cfg.rss.append(nu)
            feeds_added += 1
    if proposal.rss_urls:
        notes.append(f"LLM 제안 RSS 채택 {feeds_added}/{len(proposal.rss_urls[:5])}")

    pages = [nu for nu in (normalize_url(u) for u in proposal.list_page_urls)
             if nu and registrable_domain(urlsplit(nu).netloc) == site_dom and nu not in cfg.list_pages]
    cfg.list_pages = (pages + cfg.list_pages)[:15]
    if pages:
        notes.append(f"LLM 제안 목록 페이지 {len(pages)}개 추가")
    if proposal.reasoning:
        notes.append(f"LLM 근거: {proposal.reasoning[:160]}")
    return notes


def assist_site(url_or_domain: str, *, name: Optional[str] = None, client=None, model: str = DEFAULT_MODEL,
                dry_run: bool = False, force: bool = False, http_client=None) -> tuple[SiteConfig, Optional[str]]:
    """자동 탐지 → 부족하면 Claude 제안 → 검증 → SiteConfig. (cfg, 보낸 프롬프트 또는 None)

    dry_run=True 면 Claude 를 호출하지 않고 보낼 프롬프트만 돌려준다."""
    d = Discoverer(client=http_client, verify=True)
    try:
        cfg = d.discover(url_or_domain, name=name)
        if cfg.error or d.home_tree is None:
            return cfg, None
        if cfg.has_source and not force:
            cfg.notes.append("자동 탐지로 충분해 LLM 을 쓰지 않음")
            return cfg, None
        anchors = anchors_of(d.home_tree, [cfg.home])
        ev = build_evidence(cfg.home, d.home_tree, d.page_links, anchors, d.declared_sitemaps)
        prompt = evidence_to_prompt(ev)
        if dry_run:
            cfg.notes.append(f"LLM dry-run: 프롬프트 {len(prompt)}자, 링크 {len(ev.links)}개")
            return cfg, prompt
        proposal = ask_claude(prompt, client=client, model=model)
        if proposal is None:
            cfg.notes.append("LLM 응답 없음(오류·거부·잘림)")
            return cfg, prompt
        cfg.notes.extend(apply_proposal(cfg, proposal, d.page_links, d))
        return cfg, prompt
    finally:
        d.close()
