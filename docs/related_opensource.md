# 크롤링 관련 오픈소스 조사 (2026-10-09, GitHub 별 수 기준)

GitHub 검색 API(`topic:news-crawler`, `topic:news-scraper`, 이름·설명 검색, 한국어 검색)와 대표 저장소 직접 조회로 모았다.
별 수와 최근 커밋은 2026-10-09 기준. 조회수·다운로드 수는 저장소 주인만 볼 수 있어 별·포크·최근 커밋·PyPI 다운로드로 가늠했다.

## 1. 범용 뉴스 크롤러 — 우리와 같은 목표

| 저장소 | 별 | 최근 커밋 | 방식 | 우리와 다른 점 |
|---|---|---|---|---|
| [fhamborg/news-please](https://github.com/fhamborg/news-please) | 2,492 | 2026-04 | 사이트맵·RSS·링크 추적으로 주소를 모으고 newspaper/readability 로 본문 추출. "그냥 되는" 범용 뉴스 크롤러 | **가장 가까운 선행 사례.** 영어권 중심. 한국 인코딩·바이라인·CMS 경로 추측, 기사 패턴 검증, 차단 쿨다운, LLM 보조는 없음 |
| [kotartemiy/newscatcher](https://github.com/kotartemiy/newscatcher) | 2,989 | 2020-10 | RSS 기반 수집 | 2020년 이후 멈춤. RSS 없는 사이트는 못 함 |
| [flairNLP/fundus](https://github.com/flairNLP/fundus) | 480 | 2026-10 | **언론사마다 파서를 손으로 작성** (publisher 별 코드) | 정반대 접근. 품질은 높지만 사이트를 늘리려면 코드를 써야 한다. "코드 수정 없이 추가"라는 우리 요구와 대비되는 사례 |
| [lumyjuwon/KoreaNewsCrawler](https://github.com/lumyjuwon/KoreaNewsCrawler) | 226 | 2024-04 | 네이버 뉴스 페이지를 카테고리·날짜로 긁음 | 한국 저장소 중 최다 별. 언론사 사이트가 아니라 **네이버 뉴스 한 곳**을 긁는다 |
| [forkonlp/N2H4](https://github.com/forkonlp/N2H4) | 220 | 2026-02 | R 언어, 네이버 뉴스 수집 | 역시 네이버 전용 |
| [LuChang-CS/news-crawler](https://github.com/LuChang-CS/news-crawler) | 131 | 2022-12 | BBC·로이터·NYT 3곳 하드코딩 | 사이트별 코드 |
| [NanmiCoder/NewsCrawler](https://github.com/NanmiCoder/NewsCrawler) | 623 | 2026-08 | 중국 플랫폼(위챗·터우탸오 등) 모음 | 플랫폼별 코드 |

한국어로 검색되는 "뉴스 크롤러" 저장소는 대부분 별 10개 미만, 2019~2022년에 멈춘 네이버·다음 전용 크롤러다.
언론사 사이트를 직접 다루면서 설정만으로 늘리는 한국어 프로젝트는 찾지 못했다.

## 2. 본문 추출 라이브러리 — 우리 1단계의 부품

| 저장소 | 별 | 최근 커밋 | PyPI 월 다운로드 | 우리 사용 |
|---|---|---|---|---|
| [adbar/trafilatura](https://github.com/adbar/trafilatura) | 6,941 | 2026-10 | 1,294만 | 추출 1·3순위 |
| [codelucas/newspaper](https://github.com/codelucas/newspaper) (newspaper3k) | 15,173 | 2026-09 | - | 사실상 개발 중단. 포크인 아래 newspaper4k 를 씀 |
| [AndyTheFactory/newspaper4k](https://github.com/AndyTheFactory/newspaper4k) | 1,150 | 2026-08 | 20만 | 추출 5순위 |
| [buriy/python-readability](https://github.com/buriy/python-readability) (readability-lxml) | 2,898 | 2026-08 | 134만 | 추출 4순위 |
| [scrapinghub/article-extraction-benchmark](https://github.com/scrapinghub/article-extraction-benchmark) | 378 | 2026-05 (보관) | - | 추출기 비교 벤치마크. 우리는 한국 사이트 196곳으로 자체 평가(`extract_quality.md`) |

## 3. 크롤링 프레임워크 — 범용 도구

| 저장소 | 별 | 최근 커밋 | 비고 |
|---|---|---|---|
| [scrapy/scrapy](https://github.com/scrapy/scrapy) | 64,687 | 2026-10 | 파이썬 크롤링 프레임워크의 표준. 사이트마다 spider 코드를 쓰는 구조라 우리 요구와는 결이 다름 |
| [apify/crawlee-python](https://github.com/apify/crawlee-python) | 9,585 | 2026-10 | 브라우저 자동화 포함 크롤러 라이브러리 |
| [alirezamika/autoscraper](https://github.com/alirezamika/autoscraper) | 8,040 | 2026-07 | 예시 값을 주면 추출 규칙을 스스로 학습. 우리 2단계 "링크에서 패턴 추론"과 발상이 비슷 |

## 4. LLM 기반 크롤러 — 최근 흐름

| 저장소 | 별 | 최근 커밋 | 비고 |
|---|---|---|---|
| [firecrawl/firecrawl](https://github.com/firecrawl/firecrawl) | 189,781 | 2026-10 | 웹 → LLM 용 마크다운. SaaS + 오픈소스 |
| [unclecode/crawl4ai](https://github.com/unclecode/crawl4ai) | 85,061 | 2026-10 | Playwright 기반, LLM 친화 출력. **JS 렌더링 사이트(6단계)를 할 때 참고 대상** |
| [ScrapeGraphAI/Scrapegraph-ai](https://github.com/ScrapeGraphAI/Scrapegraph-ai) | 31,647 | 2026-10 | LLM 이 추출 규칙을 만듦. 우리 7단계(LLM 보조 설정)와 발상이 같지만, 우리는 제안을 코드가 검증한 뒤에만 채택 |

## 우리 프로젝트의 자리

- **가장 가까운 선행 사례는 news-please** (범용·설정 중심). 우리는 거기에 한국 사이트 특화(EUC-KR, 한국식 날짜·바이라인, ndsoft CMS RSS 경로, RSS 안내 페이지 수집),
  기사 URL 패턴의 **샘플 추출 검증**, 차단 감지와 **24시간 쿨다운 영속화**, 탐지 실패 시 **LLM 제안 + 코드 검증**을 더했다.
- **fundus 는 반대편 극단**(언론사마다 코드). 발표 때 "왜 설정 방식을 택했나"를 설명할 때 대비 사례로 쓸 수 있다.
- **한국어 오픈소스는 네이버 뉴스 전용이 거의 전부**다. 언론사 246곳을 직접 조사해 222곳을 설정 파일로 돌리는 사례는 찾지 못했다.
- **배울 점**: crawl4ai 의 Playwright 통합(6단계), article-extraction-benchmark 의 평가 방법, autoscraper 의 예시 기반 규칙 학습.
