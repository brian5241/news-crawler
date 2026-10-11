from types import SimpleNamespace

from crawler.discover import SiteConfig
from crawler.llm_assist import Evidence, Proposal, apply_proposal, ask_claude, evidence_to_prompt

LINKS = [f"https://www.site.co.kr/news/articleView.html?idxno=1000{i}" for i in range(6)] + \
        ["https://www.site.co.kr/news/articleList.html?sc_section_code=S1N1", "https://www.site.co.kr/economy",
         "https://www.site.co.kr/politics", "https://other.com/news/articleView.html?idxno=99999"]


class FakeDiscoverer:
    verify = True
    def __init__(self, ok=True):
        self.ok = ok
        self.verified = []
    def _verify_article(self, url):
        self.verified.append(url)
        return self.ok
    def _check_feed(self, url):
        return ("feed", ["https://www.site.co.kr/news/articleView.html?idxno=1"], "") if url.endswith(".xml") else ("bad", [], "")


def test_prompt_groups_links_by_template():
    ev = Evidence(home="https://www.site.co.kr/", title="사이트", site_name="사이트", links=[(u, "앵커") for u in LINKS[:7]])
    prompt = evidence_to_prompt(ev)
    assert "내부 링크 7개" in prompt and "| 6" in prompt   # 같은 꼴 6개 중 대표 4개만
    assert prompt.count("articleView") == 4


def test_apply_proposal_adopts_verified_regex_and_rejects_weak_ones():
    cfg = SiteConfig(name="사이트", domain="site.co.kr", home="https://www.site.co.kr/")
    good = r"^https?://(?:www\.)?site\.co\.kr/news/articleView\.html\?idxno=\d+(?:[?#&].*)?$"
    weak = r"^https?://(?:www\.)?site\.co\.kr/economy$"        # 1개만 맞음
    bad = r"^https?://(?:www\.)?site\.co\.kr/(news"             # 문법 오류
    p = Proposal(article_url_regexes=[bad, weak, good], list_page_urls=["https://www.site.co.kr/economy", "https://other.com/x"],
                 rss_urls=["https://www.site.co.kr/rss/all.xml", "https://www.site.co.kr/rss"], reasoning="idxno 가 기사 ID")
    d = FakeDiscoverer()
    notes = apply_proposal(cfg, p, LINKS, d)
    assert cfg.article_patterns == [good] and d.verified == [LINKS[0]]
    assert cfg.rss == ["https://www.site.co.kr/rss/all.xml"]
    assert cfg.list_pages == ["https://www.site.co.kr/economy"]
    assert any("무효" in n for n in notes) and any("1개만" in n for n in notes) and "LLM 제안 기사 패턴 채택 1개" in notes


def test_apply_proposal_falls_back_to_examples_when_regex_fails():
    cfg = SiteConfig(name="사이트", domain="site.co.kr", home="https://www.site.co.kr/")
    p = Proposal(article_url_regexes=[r"^https?://nothing\.matches$"], example_article_urls=LINKS[:4] + ["https://www.site.co.kr/made/up/123456"])
    apply_proposal(cfg, p, LINKS, FakeDiscoverer())
    assert len(cfg.article_patterns) == 1 and r"idxno=\d+" in cfg.article_patterns[0]


def test_apply_proposal_drops_patterns_when_verification_fails():
    cfg = SiteConfig(name="사이트", domain="site.co.kr", home="https://www.site.co.kr/")
    p = Proposal(article_url_regexes=[r"^https?://(?:www\.)?site\.co\.kr/news/articleView\.html\?idxno=\d+(?:[?#&].*)?$"])
    notes = apply_proposal(cfg, p, LINKS, FakeDiscoverer(ok=False))
    assert cfg.article_patterns == [] and any("검증 실패" in n for n in notes)


def test_ask_claude_uses_parse_and_handles_refusal():
    captured = {}
    class FakeMessages:
        def parse(self, **kw):
            captured.update(kw)
            return SimpleNamespace(stop_reason="end_turn", parsed_output=Proposal(reasoning="ok"))
    client = SimpleNamespace(messages=FakeMessages())
    out = ask_claude("증거", client=client)
    assert out.reasoning == "ok"
    assert captured["model"] == "claude-opus-5-5" and captured["output_format"] is Proposal
    assert captured["output_config"] == {"effort": "high"} and captured["messages"][0]["content"] == "증거"

    class RefusingMessages:
        def parse(self, **kw):
            return SimpleNamespace(stop_reason="refusal", parsed_output=None, stop_details=None)
    assert ask_claude("증거", client=SimpleNamespace(messages=RefusingMessages())) is None
