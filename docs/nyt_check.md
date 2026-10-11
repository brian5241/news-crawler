# 뉴욕타임스(nytimes.com) 수집 가능성 점검 (2026-10-09)

요청은 정확히 세 번만 보냈다. 재시도는 껐고, 첫 차단 신호에서 멈췄다. 자동 탐지(`discover`, 요청 20~30회)는 돌리지 않았다.

| 순서 | 요청 | 결과 |
|---|---|---|
| 1 | `https://www.nytimes.com/robots.txt` | HTTP 200. `User-agent: *` 에는 금지 규칙이 없다. 대신 이름을 지정해 막는 봇이 많다: Googlebot(일부 경로), anthropic-ai, ChatGPT-User, CCBot, Bytespider, Amazonbot, archive.org_bot 등. 사이트맵 5개 선언 (`sitemaps/new/news.xml.gz` 등) |
| 2 | 공식 RSS `rss.nytimes.com/services/xml/rss/nyt/HomePage.xml` | HTTP 200, 항목 16개. 제목·요약·링크만 들어 있다 |
| 3 | RSS 첫 기사 1건 | **HTTP 403, 771바이트.** 본문 대신 Datadome 봇 검사 페이지(`captcha-delivery.com`, "Please enable JS and disable any ad blocker") |

## 판정

- **기사 본문은 정적 요청으로 수집할 수 없다.** 첫 기사부터 Datadome 이 막았다. 브라우저 지문·JS 실행·쿠키를 보고 거르는 방식이라, 헤더를 바꾸는 정도로는 안 열리고 반복하면 IP 단위 차단으로 번진다.
- robots.txt 만 보면 일반 크롤러에게 기사 경로가 열려 있지만, NYT 이용약관은 자동 수집을 금지하고 실제로 소송을 낸 전력이 있다. 과제 범위에 넣지 않는 것이 맞다.
- 쓸 수 있는 것은 **공식 RSS(제목·요약·링크)** 뿐이다. 본문은 없다.
- "처음엔 보이다가 몇 건 뒤에 막히는" 계량 페이월은 확인할 수 없었다. 그 전 단계(봇 검사)에서 막혀서다.

## 이 점검을 계기로 넣은 안전장치 (코드)

1. `crawler/fetch.py` `detect_block()`: 403, 429, 그리고 Datadome·Cloudflare·PerimeterX·Incapsula·Akamai 류 봇 검사 페이지 흔적을 응답에서 알아본다. 429 는 재시도하지 않는다.
2. `crawler/sources.py` `Collector`: 차단 응답이 **한 번**이라도 오면 그 사이트에 더 요청하지 않는다(네트워크 오류 2회 연속 중단과 별도).
3. `crawler/pipeline.py`: 차단이 감지되면 DB `site_state` 에 쿨다운(기본 24시간, 서버가 `Retry-After` 를 주면 그 이상)을 적고, 다음 실행에서도 그 시각까지 그 사이트를 건너뛴다. `python main.py stats` 에서 목록을 보고, 풀렸다고 확인했을 때만 `python main.py unblock 사이트키` 로 해제한다.
4. `crawler/discover.py`: 자동 탐지 중 차단 응답이 오면 남은 후보 요청을 모두 생략하고 찾은 것만 기록한다.

nytimes.com 은 `sites.yaml` 에 넣지 않았다.
