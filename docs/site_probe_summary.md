# 네이버 뉴스스탠드 246개 언론사 1차 자동 조사 (2026-10-07)

방법: 홈페이지 접속 → RSS 자동 탐지(link 태그·href·경로 추측 11종) → 기사 1건 선택(RSS 우선, 없으면 홈페이지 링크 패턴) → trafilatura 본문 추출 → 유료 키워드 검색.

| 분류 | 추출성공 | 본문짧음 | 추출실패 | 기사URL못찾음 | 접속불가 | 합계 |
|---|---|---|---|---|---|---|
| 종합/방송/통신 | 36 | 1 | 4 | 10 | 1 | 52 |
| 경제/IT | 49 | 0 | 1 | 5 | 0 | 55 |
| 지역 | 16 | 0 | 1 | 1 | 1 | 19 |
| 전문지 | 52 | 1 | 3 | 4 | 1 | 61 |
| 인터넷 | 29 | 0 | 2 | 3 | 0 | 34 |
| 스포츠/연예 | 8 | 0 | 1 | 5 | 0 | 14 |
| 매거진 | 8 | 0 | 0 | 0 | 0 | 8 |
| 영자/외국어 | 2 | 0 | 0 | 1 | 0 | 3 |
| **합계** | **200** | 2 | 12 | 29 | 3 | 246 |

- RSS 자동 발견: 151/246
- 유료 키워드 신호: 27개. 전부 본문이 정상 추출됨 → 푸터·구독 배너 오탐으로 판단.

## 1차 실패 46곳 (원인별)

**기사URL못찾음 29곳**: 뉴시스(www.newsis.com), 오마이뉴스(www.ohmynews.com), YTN(www.ytn.co.kr), SBS(news.sbs.co.kr/indexes/news_index.html), KBS(news.kbs.co.kr), 노컷뉴스(www.nocutnews.co.kr), 부산일보(www.busan.com), MBC(imnews.imbc.com), 한국경제TV(www.wowtv.co.kr), 국제신문(www.kookje.co.kr), 게임메카(www.gamemeca.com), 조선비즈(biz.chosun.com), SBS Biz(biz.sbs.co.kr), 영남일보(www.yeongnam.com), 뉴스엔(www.newsen.com), 더팩트(www.tf.co.kr), 월간노동법률(www.worklaw.co.kr), 국방일보(kookbang.dema.mil.kr), 아시아투데이(asiatoday.co.kr), Arirang TV(www.arirang.com), 나우뉴스(nownews.seoul.co.kr), YTN사이언스(science.ytn.co.kr), YONHAPNEWS(en.yna.co.kr), 대한경제(www.dnews.co.kr), 에너지경제(www.ekn.kr), 코리아쉬핑가제트(www.ksg.co.kr), 한국금융신문(www.fntimes.com), TBS(tbs.seoul.kr/news/index.do), TBC(www.tbc.co.kr/tbc_news/news_main14.html)

**추출실패 12곳**: 국민일보(www.kmib.co.kr/news/index.asp), 머니투데이(www.mt.co.kr), 한국경제(www.hankyung.com), 매경이코노미(economy.mk.co.kr), 광주일보(www.kwangju.co.kr), KBS World(world.kbs.co.kr/english), 동아사이언스(www.dongascience.com), 동행미디어 시대(www.sidae.com), 바이라인네트워크(byline.network), 경북매일신문(www.kbmaeil.com), 머니투데이방송(news.mtn.co.kr), 스포츠투데이(stoo.com)

**본문짧음 2곳**: 조선일보(www.chosun.com), 낚시춘추(fish.darakwon.co.kr)

**접속불가 3곳**: 매일경제(www.mk.co.kr), 스타뉴스(star.mt.co.kr), 농민신문(www.nongmin.com)

## 해석

- 기사URL못찾음 29곳은 사이트가 막은 게 아니라 1차 스크립트의 기사 URL 정규식이 좁아서 생긴 것. 사이트맵 탐색과 패턴 확장으로 대부분 회수 가능.
- 추출실패 중 403은 봇 차단(한국경제, 매경이코노미). 헤더 보강 또는 Playwright 폴백 대상.
- 접속불가 3곳(매일경제, 스타뉴스, 농민신문)은 TLS/차단 재확인 필요.
- 조선일보 197자는 JS 렌더링 문제. 로그인월은 일부 프리미엄 기사만 해당.
- 유료 전용 매체(더벨, 인베스트조선 등)는 애초에 뉴스스탠드 목록에 없음.