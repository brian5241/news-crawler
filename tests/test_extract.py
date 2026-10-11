from crawler.extract import (clean_author, clean_text, clean_title, extract_from_html, looks_like_headline_list,
                             looks_like_html, normalize_date)

ARTICLE_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>국감서 AI 안전 논쟁 - 테스트신문</title>
<meta property="og:title" content="국감서 AI 안전 논쟁">
<meta property="og:site_name" content="테스트신문">
<meta property="og:description" content="국정감사에서 인공지능 안전을 두고 논쟁이 벌어졌다.">
<meta property="article:published_time" content="2026-10-09T14:32:00+09:00">
<meta name="author" content="홍길동 기자 hong@test.co.kr">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"NewsArticle","headline":"국감서 AI 안전 논쟁","datePublished":"2026-10-09T14:32:00+09:00","author":{"@type":"Person","name":"홍길동"},"inLanguage":["ko","en"]}</script>
</head><body><nav><a href="/politics">정치</a><a href="/economy">경제</a></nav>
<article><h1>국감서 AI 안전 논쟁</h1>
<p>9일 국회 과학기술정보방송통신위원회 국정감사에서 인공지능 안전을 두고 여야 의원들이 날 선 공방을 벌였다. 의원들은 정부의 인공지능 기본법 시행령 준비 상황과 안전 연구소 예산을 집중적으로 따져 물었다.</p>
<p>정부 측은 "관련 예산을 내년에 두 배로 늘리겠다"고 답했고, 야당은 "지난해 약속한 인력 충원도 아직 끝나지 않았다"며 실행력을 문제 삼았다. 전문가들은 제도보다 실제 검증 체계가 먼저라고 지적한다.</p>
<p>한편 이날 감사에서는 해킹 대응 체계도 도마에 올랐다. 위원장은 다음 주 추가 질의를 예고했다.</p>
<p>홍길동 기자 hong@test.co.kr</p>
<p>저작권자 ⓒ 테스트신문 무단전재 및 재배포 금지</p></article>
<aside><ul><li>많이 본 뉴스</li><li>어제의 핫이슈 정리</li></ul></aside></body></html>"""


def test_normalize_date_formats():
    assert normalize_date("2026-10-07T14:32:00+09:00") == "2026-10-07 14:32"
    assert normalize_date("2026-10-07T05:32:00Z") == "2026-10-07 14:32"
    assert normalize_date("2026.10.07 14:32") == "2026-10-07 14:32"
    assert normalize_date("2026년 10월 7일 오후 2시 32분") == "2026-10-07 14:32"
    assert normalize_date("입력 2026.10.07. 오전 9:05") == "2026-10-07 09:05"
    assert normalize_date("2026/10/07") == "2026-10-07"
    assert normalize_date("1999-01-01") is None
    assert normalize_date("garbage") is None and normalize_date(None) is None


def test_clean_author_variants():
    assert clean_author("권원근 기자 Kwon Co Kr") == "권원근 기자"
    assert clean_author("Www Etnews Com") is None
    assert clean_author("홍길동·김철수 기자") == "홍길동·김철수 기자"
    assert clean_author("[서울=뉴시스] 김철수") == "김철수"
    assert clean_author("남영주; Ichannela Com") == "남영주"
    assert clean_author("https://example.com/author/1") is None
    assert clean_author("John Smith") == "John Smith"


def test_clean_text_removes_boilerplate_and_trailing_byline():
    text = "본문 첫 줄\n\nⓒ 한겨레 무단전재 및 재배포 금지\n▶ 관련기사 보기\n둘째 줄\n홍길동 기자 h@h.com"
    assert clean_text(text) == "본문 첫 줄\n둘째 줄"
    junk = "SNS 기사보내기\n기사저장\n본문 글씨 줄이기\n이 기사를 공유합니다\n닫기"
    assert clean_text(junk) == ""


def test_clean_title_strips_site_suffix_only_when_asked():
    assert clean_title("제목이 길게 있는 기사입니다 - 연합뉴스", strip_suffix=True) == "제목이 길게 있는 기사입니다"
    assert clean_title("짧은 - 연합뉴스", strip_suffix=True) == "짧은 - 연합뉴스"
    assert clean_title("  공백   정리  ") == "공백 정리"


def test_headline_list_detection():
    sidebar = "\n".join(["고소영, 낮술 마시다 쫓겨나 “점심 때 4병...", "임신 김민희, 돈 없는 홍상수 불륜 진심…", "장원영 인형 비주얼",
                         "이정재 다정남‥파파라치에 걸린 11년차...", "혜리 과감하게 벗었다", "전소미 파격 노출", "차은우 군입대 D-1"])
    assert looks_like_headline_list(sidebar) is True
    article = "\n".join(["평가원은 지원사업을 시행한다.", "인증제는 탄소 배출을 줄인 농장을 인증하는 제도다.", "사업은 비용 부담을 완화하기 위해 마련됐다.",
                         "포장재에 마크를 직접 인쇄해 지원한다.", "원장은 “쉽게 식별할 수 있을 것”이라고 말했다.", "사업은 11월까지 진행된다."])
    assert looks_like_headline_list(article) is False


def test_extract_from_html_full_article():
    art = extract_from_html(ARTICLE_HTML, "https://www.test.co.kr/news/1234")
    assert art.ok and art.extractor == "trafilatura"
    assert art.title == "국감서 AI 안전 논쟁"
    assert art.date == "2026-10-09 14:32"
    assert art.author.startswith("홍길동")
    assert art.publisher == "테스트신문"
    assert art.language == "ko"
    assert "무단전재" not in art.text and "많이 본 뉴스" not in art.text
    assert art.text_length > 200


def test_extract_rejects_non_html():
    art = extract_from_html(".a{color:red}" * 300, "https://www.test.co.kr/app.css")
    assert art.ok is False and art.error == "non-HTML content" and art.text_length == 0
    assert looks_like_html('{"a": 1}') is False and looks_like_html("<html><body></body></html>") is True
