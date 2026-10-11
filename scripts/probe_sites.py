"""246개 언론사 자동 조사: 접속/RSS/기사추출/유료신호. 결과 → probe_results.csv"""
import csv, re, sys, json, glob, html
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse
import httpx, feedparser, trafilatura

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
HDR = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"}
RSS_GUESSES = ["/rss/allArticle.xml", "/rss/S1N1.xml", "/rss", "/rss.xml", "/feed", "/rss/rss.xml",
               "/rss/news.xml", "/rss/all.xml", "/feed/", "/rss/clickTop.xml", "/rss/index.xml"]
PAY_RE = re.compile(r"(유료\s*(회원|구독|기사|콘텐츠)|구독자\s*전용|멤버십\s*전용|로그인\s*후\s*(이용|열람)|paywall|subscriber[- ]only|프리미엄\s*기사|유료기사)", re.I)
ART_RE = re.compile(r"(articleView\.html\?idxno=\d+|/news/articleView|/article/|/articles/|/view/\d|/news/view|/read\.|/newsView/|/news/\d{4,}|/\d{6,}|aid=\d+|idxno=\d+|no=\d{4,})", re.I)

def get(client, url):
    try:
        r = client.get(url, headers=HDR, follow_redirects=True, timeout=12)
        return r
    except Exception as e:
        return None

def find_rss(client, home, page_html):
    links = re.findall(r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', page_html or "", re.I)
    for l in links:
        m = re.search(r'href=["\']([^"\']+)', l)
        if m: return urljoin(home, html.unescape(m.group(1))), "link-tag"
    m = re.search(r'href=["\']([^"\']*rss[^"\']*\.xml)["\']', page_html or "", re.I)
    if m: return urljoin(home, html.unescape(m.group(1))), "href-rss"
    for g in RSS_GUESSES:
        r = get(client, urljoin(home, g))
        if r is not None and r.status_code == 200 and ("<rss" in r.text[:2000] or "<feed" in r.text[:2000]):
            return str(r.url), "guess"
    return "", ""

def pick_article(client, home, page_html, rss):
    if rss:
        try:
            fp = feedparser.parse(rss)
            for e in fp.entries[:3]:
                if e.get("link"): return e["link"], "rss"
        except Exception: pass
    cands = re.findall(r'href=["\']([^"\'#]+)["\']', page_html or "")
    dom = urlparse(home).netloc.replace("www.", "")
    for c in cands:
        u = urljoin(home, html.unescape(c))
        if dom in urlparse(u).netloc and ART_RE.search(u):
            return u, "homepage"
    return "", ""

def probe(row):
    pid, name, home, paper, cat = row
    out = dict(id=pid, name=name, home=home, cat=cat, status="", final="", rss="", rss_how="",
               article="", art_how="", title="", body_len=0, date="", paywall="", note="")
    with httpx.Client() as client:
        r = get(client, home)
        if r is None:
            out["status"] = "ERR"; out["note"] = "homepage unreachable"; return out
        out["status"] = r.status_code; out["final"] = str(r.url)
        page = r.text if r.status_code == 200 else ""
        rss, how = find_rss(client, str(r.url), page); out["rss"], out["rss_how"] = rss, how
        art, ahow = pick_article(client, str(r.url), page, rss); out["article"], out["art_how"] = art, ahow
        if art:
            ra = get(client, art)
            if ra is not None and ra.status_code == 200:
                h = ra.text
                out["paywall"] = "signal" if PAY_RE.search(h) else ""
                try:
                    j = trafilatura.extract(h, url=art, output_format="json", with_metadata=True, include_comments=False)
                    if j:
                        d = json.loads(j)
                        out["title"] = (d.get("title") or "")[:60]
                        out["body_len"] = len(d.get("text") or "")
                        out["date"] = d.get("date") or ""
                except Exception as e:
                    out["note"] = f"extract err {e}"
            else:
                out["note"] = f"article http {getattr(ra,'status_code','ERR')}"
    return out

def main():
    rows = []
    cats = {}
    for f in glob.glob("ns_ct*.html"):
        s = open(f, encoding="utf-8", errors="ignore").read()
        i = s.find("pressList = [")
        if i < 0: continue
        chunk = s[i+len("pressList = "):]; j = chunk.find("}];")
        for p in json.loads(chunk[:j+2]): cats.setdefault(p["id"], f.split("_")[1].split(".")[0])
    for line in open("newsstand_all.tsv", encoding="utf-8"):
        pid, name, home, paper = line.rstrip("\n").split("\t")
        if home and home != "None": rows.append((pid, name, home, paper, cats.get(pid, "")))
    print("sites:", len(rows), flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(probe, r): r for r in rows}
        for n, fu in enumerate(as_completed(futs), 1):
            res = fu.result(); results.append(res)
            print(f"[{n}/{len(rows)}] {res['name']:<14} {res['status']!s:<4} rss={'Y' if res['rss'] else '-'} body={res['body_len']:<6} pay={res['paywall'] or '-'}", flush=True)
    results.sort(key=lambda r: r["id"])
    with open("probe_results.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys())); w.writeheader(); w.writerows(results)
    ok = [r for r in results if r["body_len"] >= 300]
    print("\nDONE. reachable:", sum(1 for r in results if r["status"] == 200),
          "rss:", sum(1 for r in results if r["rss"]), "extract>=300chars:", len(ok),
          "paywall-signal:", sum(1 for r in results if r["paywall"]))

if __name__ == "__main__":
    main()
