# 보류 사이트 목록 (일부러 sites.yaml 에 넣지 않음)

작성 2026-10-09. 1차 전체 탐지에서 빠진 24곳. 이 중 B 그룹은 **코드 수정 없이 사이트가 추가되는지 시험하는 용도**로 남겨 둔다.
나중에 추가할 때는 아래 명령 한 줄씩이면 된다. (`--only-failed` 일괄 재실행은 쓰지 않는다.)

> **2026-10-09 시험 완료**: 차단이 풀린 뒤 B 그룹 20곳을 코드 수정 없이 `add-site` 한 줄씩으로 전부 추가했고(20/20), 각 사이트에서 기사 3건씩 수집까지 성공했다. 결과표는 [holdout_test.md](holdout_test.md). 이 20곳은 이제 `sites.yaml` 에 등록돼 있다.

## B. 일시 차단 — 코드 수정 없는 추가 시험용 (20곳)

2026-10-08 전체 탐지를 같은 날 세 번 반복하자 같은 호스팅 계열 사이트가 nginx 403을 돌려줌. 1차 조사(10-07)와 첫 전체 탐지에서는 정상이었음. 차단이 풀린 뒤 add-site 로 추가.

| 언론사 | 홈 | 탐지 당시 오류 | 추가 명령 |
|---|---|---|---|
| 맥스무비 | http://www.maxmovie.com/ | HTTP 403 | `python main.py add-site http://www.maxmovie.com/ --name 맥스무비` |
| 법률신문 | https://www.lawtimes.co.kr/ | HTTP 403 | `python main.py add-site https://www.lawtimes.co.kr/ --name 법률신문` |
| 제주의소리 | http://www.jejusori.net/ | HTTP 403 | `python main.py add-site http://www.jejusori.net/ --name 제주의소리` |
| 전남일보 | http://www.jnilbo.com | HTTP 403 | `python main.py add-site http://www.jnilbo.com --name 전남일보` |
| 인더스트리뉴스 | http://www.industrynews.co.kr/ | HTTP 403 | `python main.py add-site http://www.industrynews.co.kr/ --name 인더스트리뉴스` |
| 신아일보 | http://www.shinailbo.co.kr/ | HTTP 403 | `python main.py add-site http://www.shinailbo.co.kr/ --name 신아일보` |
| 법률방송뉴스 | http://www.ltn.kr/ | HTTP 403 | `python main.py add-site http://www.ltn.kr/ --name 법률방송뉴스` |
| 시사오늘 | http://www.sisaon.co.kr/ | HTTP 403 | `python main.py add-site http://www.sisaon.co.kr/ --name 시사오늘` |
| 경남도민일보 | http://www.idomin.com/ | HTTP 403 | `python main.py add-site http://www.idomin.com/ --name 경남도민일보` |
| 기호일보 | http://www.kihoilbo.co.kr/ | HTTP 403 | `python main.py add-site http://www.kihoilbo.co.kr/ --name 기호일보` |
| 르몽드 디플로마티크 | http://ilemonde.com/ | HTTP 403 | `python main.py add-site http://ilemonde.com/ --name 르몽드 디플로마티크` |
| 보안뉴스 | http://www.boannews.com/ | HTTP 403 | `python main.py add-site http://www.boannews.com/ --name 보안뉴스` |
| CNB뉴스 | http://www.cnbnews.com/ | HTTP 403 | `python main.py add-site http://www.cnbnews.com/ --name CNB뉴스` |
| 독서신문 | http://www.readersnews.com/ | HTTP 403 | `python main.py add-site http://www.readersnews.com/ --name 독서신문` |
| PD저널 | http://www.pdjournal.com/ | HTTP 403 | `python main.py add-site http://www.pdjournal.com/ --name PD저널` |
| 경북도민일보 | http://www.hidomin.com/ | HTTP 403 | `python main.py add-site http://www.hidomin.com/ --name 경북도민일보` |
| 약사공론 | http://www.kpanews.co.kr | HTTP 403 | `python main.py add-site http://www.kpanews.co.kr --name 약사공론` |
| AI타임스 | http://www.aitimes.com/ | HTTP 403 | `python main.py add-site http://www.aitimes.com/ --name AI타임스` |
| 뉴스펭귄 | https://www.newspenguin.com/ | HTTP 403 | `python main.py add-site https://www.newspenguin.com/ --name 뉴스펭귄` |
| 뉴스앤조이 | https://www.newsnjoy.or.kr/ | HTTP 403 | `python main.py add-site https://www.newsnjoy.or.kr/ --name 뉴스앤조이` |

## A. Cloudflare 봇 차단 (2곳)

브라우저형 헤더로도 403. 6단계 Playwright 또는 TLS 지문 흉내(curl_cffi) 필요.

| 언론사 | 홈 | 탐지 당시 오류 | 추가 명령 |
|---|---|---|---|
| 매일경제 | http://www.mk.co.kr/ | HTTP 403 | `python main.py add-site http://www.mk.co.kr/ --name 매일경제` |
| 매경이코노미 | http://economy.mk.co.kr/ | 홈페이지가 HTML이 아님 (HTTP 200, text/html, 80바이트) | `python main.py add-site http://economy.mk.co.kr/ --name 매경이코노미` |

## C. 처음부터 403 (지속 차단 추정) (0곳)

1차 조사부터 403. 봇 차단 또는 국내 IP 대역 외 차단 가능성. 6단계에서 재확인.

| 언론사 | 홈 | 탐지 당시 오류 | 추가 명령 |
|---|---|---|---|

## D. JS 렌더링 (HTML 안에 기사 링크 없음, RSS·사이트맵 없음) (2곳)

정적 요청으로는 기사 주소를 얻을 수 없음. 6단계 Playwright 대상.

| 언론사 | 홈 | 탐지 당시 오류 | 추가 명령 |
|---|---|---|---|
| 스타뉴스 | http://star.mt.co.kr/ | 기사 URL 패턴 후보 없음 / 요청 25회, 4.0초 | `python main.py add-site http://star.mt.co.kr/ --name 스타뉴스` |
| Arirang TV | https://www.arirang.com/ | 기사 URL 패턴 후보 없음 / 요청 25회, 5.5초 | `python main.py add-site https://www.arirang.com/ --name Arirang TV` |
