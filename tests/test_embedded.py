import json

from crawler.embedded import article_from_embedded, balanced_json, embedded_links, find_embedded_json
from crawler.extract import extract_from_html

PARA = ("국정감사에서 인공지능 안전을 두고 여야 의원들이 날 선 공방을 벌였다. 정부는 내년 예산을 두 배로 늘리겠다고 답했다. "
        "야당은 지난해 약속한 인력 충원이 아직 끝나지 않았다며 실행력을 문제 삼았고, 위원장은 다음 주 추가 질의를 예고했다.")
ARC_HTML = ("<html><head><title>t</title></head><body><div id='fusion-app'></div><script>window.Fusion=window.Fusion||{};"
            "Fusion.globalContent=" + json.dumps({
                "_id": "X", "headlines": {"basic": "국감 AI 안전 논쟁"}, "display_date": "2026-10-09T05:00:00Z",
                "credits": {"by": [{"name": "홍길동"}, {"name": "김철수"}]},
                "content_elements": [{"type": "text", "content": "<p>" + PARA + "</p>"}, {"type": "image", "url": "x.jpg"},
                                     {"type": "text", "content": PARA + " 전문가들은 검증 체계가 먼저라고 지적한다."}]
            }, ensure_ascii=False) + ";Fusion.globalContentConfig={\"source\":\"content-api\"};</script></body></html>")
NEXT_HTML = ("<html><head></head><body><div id='__next'></div><script id='__NEXT_DATA__' type='application/json'>" + json.dumps({
    "props": {"pageProps": {"article": {"title": "넥스트 기사", "publishedAt": "2026-10-09 10:00", "writer": {"name": "박영희"},
                                        "paragraphs": [PARA, PARA + " 두 번째 문단이다.", PARA + " 세 번째 문단이다."],
                                        "related": ["https://www.site.co.kr/news/1001", "https://www.site.co.kr/news/1002"]}}},
    "page": "/news/[id]"}, ensure_ascii=False) + "</script></body></html>")


def test_balanced_json_ignores_braces_in_strings():
    s = 'x={"a":"}{","b":[1,{"c":2}]};rest'
    assert balanced_json(s, 2) == '{"a":"}{","b":[1,{"c":2}]}'
    assert balanced_json("nope", 0) is None


def test_find_embedded_json_sources():
    labels = [l for l, _ in find_embedded_json(ARC_HTML)]
    assert "Fusion.globalContent" in labels
    labels = [l for l, _ in find_embedded_json(NEXT_HTML)]
    assert labels == ["__NEXT_DATA__"]


def test_arc_xp_article():
    a = article_from_embedded(ARC_HTML)
    assert a["title"] == "국감 AI 안전 논쟁" and a["author"] == "홍길동, 김철수" and a["date"] == "2026-10-09T05:00:00Z"
    assert a["text"].count("\n") == 1 and "<p>" not in a["text"] and a["text"].startswith(PARA)


def test_generic_json_article_and_links():
    a = article_from_embedded(NEXT_HTML)
    assert a["title"] == "넥스트 기사" and a["author"] == "박영희" and a["date"] == "2026-10-09 10:00"
    assert a["text"].count("\n") == 2
    assert embedded_links(NEXT_HTML, "site.co.kr") == ["https://www.site.co.kr/news/1001", "https://www.site.co.kr/news/1002"]
    assert embedded_links(NEXT_HTML, "other.com") == []


def test_extract_uses_embedded_when_html_body_is_empty():
    art = extract_from_html(ARC_HTML, "https://www.chosun.com/x/2026/10/09/ABC/")
    assert art.ok and art.extractor == "embedded"
    assert art.title == "국감 AI 안전 논쟁" and art.author == "홍길동, 김철수" and art.date == "2026-10-09 14:00"
    assert art.text_length > 200
