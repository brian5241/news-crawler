# 기사 추출 모듈 품질 평가 (2026-10-07)

대상: 1차 조사에서 URL을 확보한 214개 사이트, 사이트당 1건. 이 중 기사 URL 196곳으로 성공률을 계산하고, 기사가 아닌 링크 18곳은 맨 아래 따로 적는다.
기준: 본문 80자 이상이면 성공 (200자 미만이면 다음 추출기도 시도).

**기사 URL 기준 성공 193/196 (98%)**

## 어느 단계에서 뽑혔나 (기사 URL 기준)

| 추출기 | 사이트 수 |
|---|---|
| trafilatura | 189 |
| none | 2 |
| meta-only | 1 |
| trafilatura:recall:short | 1 |
| readability | 1 |
| readability:short | 1 |
| trafilatura:recall | 1 |

`*:short` = 모든 추출기가 200자 미만이라 가장 긴 결과를 채택(포토 기사 등). `meta-only` = 본문 없음, 메타데이터만.

## 성공한 기사의 메타데이터 채움률

| 항목 | 채움 | 비율 |
|---|---|---|
| 제목 | 193/193 | 100% |
| 날짜 | 193/193 | 100% |
| 날짜(시각 포함) | 167/193 | 87% |
| 기자 | 185/193 | 96% |
| 언론사명(도메인 아닌 이름) | 174/193 | 90% |

## 페이지 인코딩 분포 (기사 URL 기준)

| 인코딩 | 사이트 수 |
|---|---|
| utf-8-sig | 189 |
| cp949 | 4 |
| utf-8/replace | 3 |

`utf-8/replace` = utf-8·선언 charset·cp949 엄격 디코딩이 모두 실패해 깨진 바이트만 치환한 경우.

## 분류별 (기사 URL 기준)

| 분류 | 대상 | 성공 | 비율 |
|---|---|---|---|
| 전문지 | 50 | 50 | 100% |
| 경제/IT | 48 | 48 | 100% |
| 종합/방송/통신 | 37 | 35 | 95% |
| 인터넷 | 29 | 28 | 97% |
| 지역 | 15 | 15 | 100% |
| 매거진 | 8 | 8 | 100% |
| 스포츠/연예 | 7 | 7 | 100% |
| 영자/외국어 | 2 | 2 | 100% |

## 정밀도 모드와 재현율 모드 길이 차이가 2배 이상인 곳 (0곳, 수동 확인 후보)


## 추출 실패 3곳 (기사 URL인데 본문을 못 뽑음)

| 언론사 | HTTP | 추출기 | 본문길이 | 오류 | URL |
|---|---|---|---|---|---|
| 한국경제 | 403 | none |  | HTTP 403 | https://www.hankyung.com/premium9/0100001 |
| KBS World | 404 | none |  | HTTP 404 | http://world.kbs.co.kr/vietnamese/news/news_Po_detail.htm?No=32886 |
| 경남신문 | 200 | meta-only | 0 |  | http://www.knnews.co.kr/news/articleView.php?idxno=1067243 |

## 기사 아닌 URL 18곳 (1차 조사 스크립트의 링크 선택 문제, 2단계 탐지 모듈에서 해결)

성공률 계산에서 제외. 목록·태그 페이지는 본문이 '뽑히긴' 하지만 기사가 아니다.

| 언론사 | 추출기 | 본문길이 | URL |
|---|---|---|---|
| 머니투데이 | none | 0 | https://menu.mt.co.kr/mt24/assets/20261006155555/app-BcUzOlo9.css |
| 조선일보 | trafilatura:short | 197 | https://www.chosun.com/members-event/?mec=event&mes=exception/1259787_7736 |
| 매경이코노미 | none |  | https://www.mk.co.kr/rss/ |
| 아이뉴스24 | trafilatura | 3400 | http://www.inews24.com/list/020100 |
| 경향신문 | trafilatura | 4925 | https://www.khan.co.kr/issue/articles/ah976 |
| 한경비즈니스 | trafilatura | 13347 | https://magazine.hankyung.com/business/tag/20160812182448963 |
| 광주일보 | none | 0 | http://www.kwangju.co.kr/ad_view.php?aid=1471 |
| 동아사이언스 | none | 0 | https://image.dongascience.com/Photo/2026/10/17908158084825.jpg |
| 스포츠월드 | trafilatura | 542 | https://www.sportsworldi.com/list/1105000000000 |
| 동행미디어 시대 | none | 0 | https://menu.sidae.com/moneyweek/thumb/2026/10/06/06/2026100614250060917_1.jpg |
| 한경잡앤조이 | trafilatura | 13048 | https://magazine.hankyung.com/job-joy/tag/20210224174448473 |
| 한경머니 | trafilatura | 13797 | https://magazine.hankyung.com/money/tag/20160812182448963,20210120135336433,20170213142515746 |
| 바이라인네트워크 | readability | 411 | https://byline.network/wp-json/wp/v2/pages/9004111222523271 |
| 경북매일신문 | none |  | https://www.kbmaeil.com/null/20261006500675 |
| 머니투데이방송 | none | 0 | https://news.mtn.co.kr/_next/static/css/5796637433558ea7.css |
| 인민망 | none | 0 | http://kr.people.com.cn/img/FOREIGN/2021/04/313138/source/css/flexible.css |
| 조이뉴스24 | trafilatura | 2707 | https://www.joynews24.com/list/708100 |
| KBC광주방송 | trafilatura | 4540 | https://news.ikbc.co.kr/article/list/kbc001000000 |
