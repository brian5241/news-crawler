"""보류 집합(docs/holdout_sites.md 의 B 그룹) '코드 수정 없이 추가' 시험.

실제 사용자 흐름 그대로 `python main.py add-site 주소 --name 이름` 을 한 곳씩 순서대로 실행하고(사이트 사이 15초 휴식),
추가된 사이트에서 `crawl --only ... --limit-per-site 3` 으로 기사를 실제로 받아 본다.
출력: docs/holdout_test.md
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
GAP_BETWEEN_SITES = 8


def parse_holdout() -> list[tuple[str, str]]:
    text = open(os.path.join(ROOT, "docs", "holdout_sites.md"), encoding="utf-8").read()
    section = text.split("## B.", 1)[1].split("\n## ", 1)[0]
    out = []
    for m in re.finditer(r"`python main\.py add-site (\S+) --name ([^`]+)`", section):
        out.append((m.group(1), m.group(2).strip()))
    return out


def run(cmd: list[str]) -> tuple[int, str]:
    env = dict(os.environ, PYTHONUTF8="1")
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=env)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sites = parse_holdout()
    print(f"보류 사이트 {len(sites)}곳", flush=True)
    rows = []
    for i, (home, name) in enumerate(sites, 1):
        t0 = time.monotonic()
        code, out = run([PY, "main.py", "add-site", home, "--name", name])
        rss = re.search(r"RSS\s*:\s*(\d+)개", out)
        maps = re.search(r"사이트맵\s*:\s*(\d+)개 \(뉴스 사이트맵 (\d+)개\)", out)
        pats = re.search(r"기사 패턴:\s*(\d+)개", out)
        ok = "수집 가능: 예" in out
        saved = "추가:" in out or "교체:" in out
        err = re.search(r"오류\s*:\s*(.+)", out)
        key = re.search(r"(?:추가|교체): \S+ ← (\S+)", out)
        rows.append(dict(name=name, home=home, ok=ok, saved=saved, key=key.group(1) if key else "",
                         rss=int(rss.group(1)) if rss else 0, sitemaps=int(maps.group(1)) if maps else 0,
                         news_sitemaps=int(maps.group(2)) if maps else 0, patterns=int(pats.group(1)) if pats else 0,
                         error=(err.group(1).strip() if err else ""), secs=round(time.monotonic() - t0, 1), stored=None))
        r = rows[-1]
        print(f"[{i}/{len(sites)}] {name:<12} 수집가능={'예' if ok else '아니오'} rss={r['rss']} map={r['sitemaps']}/{r['news_sitemaps']} "
              f"pat={r['patterns']} 저장={'O' if saved else 'X'} {r['secs']}s {r['error'][:50]}", flush=True)
        if i < len(sites):
            time.sleep(GAP_BETWEEN_SITES)

    keys = [r["key"] for r in rows if r["saved"] and r["key"]]
    if keys:
        print(f"\n추가된 {len(keys)}곳에서 기사 3건씩 수집 시험", flush=True)
        code, out = run([PY, "main.py", "crawl", "--only", ",".join(keys), "--limit-per-site", "3", "--workers", "4"])
        print(out[-1500:], flush=True)
        for r in rows:
            m = re.search(rf"\] {re.escape(r['name'])}\s+주소 (\d+)\s+새 (\d+)\s+저장 (\d+)", out)
            if m:
                r["stored"] = int(m.group(3))
                r["found"] = int(m.group(1))

    added = [r for r in rows if r["saved"]]
    L = [f"# 보류 사이트 '코드 수정 없이 추가' 시험 ({time.strftime('%Y-%m-%d')})", "",
         f"2026-10-08 일시 차단으로 보류해 둔 {len(rows)}곳을, 코드를 한 줄도 고치지 않고 `python main.py add-site 주소 --name 이름` 만으로 "
         f"한 곳씩(사이트 사이 {GAP_BETWEEN_SITES}초 휴식) 추가한 뒤, 추가된 사이트에서 기사 3건씩을 실제로 받아 봤다.", "",
         f"**추가 성공 {len(added)}/{len(rows)}곳**, 기사 수집 성공 {sum(1 for r in rows if (r['stored'] or 0) > 0)}/{len(added)}곳", "",
         "| 언론사 | 수집 가능 | RSS | 사이트맵(뉴스) | 기사 패턴 | 저장됨 | 기사 3건 시험 | 소요(초) | 오류 |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        trial = "-" if r["stored"] is None else f"{r['stored']}건 저장 (주소 {r.get('found', 0)})"
        L.append(f"| {r['name']} | {'예' if r['ok'] else '아니오'} | {r['rss']} | {r['sitemaps']}({r['news_sitemaps']}) | {r['patterns']} | "
                 f"{'O' if r['saved'] else 'X'} | {trial} | {r['secs']} | {r['error']} |")
    L += ["", "자동 탐지 로직·파이프라인·CLI 어느 것도 수정하지 않았다. 바뀐 것은 `sites.yaml` 뿐이다."]
    path = os.path.join(ROOT, "docs", "holdout_test.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(f"\nDONE added={len(added)}/{len(rows)} → {path}", flush=True)


if __name__ == "__main__":
    main()
