# 유료·로그인 벽 점검 (2026-10-10)

sites.yaml 의 222개 사이트마다 최신 기사 최대 8건을 같은 세션(쿠키 유지)으로 순서대로 읽고, 마지막에 **첫 기사를 같은 세션에서 다시 읽어** 처음엔 열렸던 기사가 잠겼는지 확인했다(계량 페이월의 결정적 시험, 205곳 수행).

| 판정 | 사이트 수 | 뜻 |
|---|---|---|
| free | 216 | 읽은 기사 전부 본문이 나왔고 잠금 신호 없음. 첫 기사 재확인도 열림 |
| partial-lock | 2 | 일부 기사만 잠김 (특정 기사에 로그인·구독 요구, 한국 언론사의 흔한 형태). 재확인은 열림 |
| metered-confirmed | 0 | 처음 열렸던 첫 기사가 8건 읽은 뒤 잠김 (뉴욕타임스식 계량 페이월) |
| js-or-empty | 4 | 대부분 본문이 짧음. 페이월이 아니라 JS 렌더링 사이트일 가능성 (6단계 Playwright 대상) |
| no-articles | 0 | 기사 주소를 모으지 못함 |

잠금 신호: `isAccessibleForFree=false`(JSON-LD 표준 표시), `login-redirect`, `http401/402/403`, 또는 `paywall-dom`(paywall/login-wall/piano 류 표식)·`paywall-html`(유료회원전용·会員限定 류 강한 문구)·`paywall-text`(본문 안의 로그인 후·구독하시면 류 문구)가 짧은 본문·티저와 함께 나타날 때. 그 사이트의 열린 긴 기사에도 똑같이 나오는 표식·문구는 공통 메뉴로 보고 제외했다(`chrome` 열).

**한계**: 자바스크립트로만 동작하는 클라이언트 측 계량(NYT 방식)은 실제 브라우저 없이는 보이지 않는다. 여기서 'free'는 서버가 HTML로 본문을 내려줬다는 뜻이다.

## 잠금 신호가 나온 2곳

| 언론사 | 판정 | 읽은 수 | 잠긴 위치 | 첫 기사 재확인 | 신호 | 공통 메뉴로 제외 | 샘플 |
|---|---|---|---|---|---|---|---|
| 식품저널 foodnews | partial-lock | 8 | 5,6,7,8 | 열림 638자 | paywall-html|paywall-text|short|teaser-only |  | http://www.foodnews.co.kr/news/articleView.html?idxno=120468 |
| 시사IN | partial-lock | 8 | 4,5,6 | 열림 1269자 | paywall-html|short|teaser-only |  | https://www.sisain.co.kr/news/articleView.html?idxno=58598 |

## 본문은 열리지만 페이월 장치나 유료 안내 문구가 있는 5곳 (프리미엄 기사만 잠그는 곳일 수 있음)

- 한국경제 (hankyung.com): paywall 표식 2/8건, 신호 paywall-dom
- 지지통신 (jiji.com): paywall 표식 8/8건, 신호 paywall-dom|paywall-html|short|teaser-only
- 중앙일보 (joongang.co.kr): paywall 표식 8/8건, 신호 paywall-dom
- 중앙SUNDAY (joongang.co.kr): paywall 표식 8/8건, 신호 paywall-dom
- 여성신문 (womennews.co.kr): paywall 표식 0/8건, 신호 paywall-html|teaser-only

## 판정 불가 4곳

- 국제신문 (kookje.co.kr): js-or-empty, 읽은 수 8, 짧은 본문 6
- 뉴스엔 (newsen.com): js-or-empty, 읽은 수 8, 짧은 본문 7
- SBS Biz (sbs.co.kr): js-or-empty, 읽은 수 8, 짧은 본문 8
- SBS연예뉴스 (sbs.co.kr): js-or-empty, 읽은 수 8, 짧은 본문 6
