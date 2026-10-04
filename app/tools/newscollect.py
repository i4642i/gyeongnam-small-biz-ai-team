"""뉴스 수집(2026-09-29) — 회사 컴퓨터 도구 news_collect. 모델을 부르지 않는다.

검색어 묶음(data/config/news_collect.json)마다 Tavily 뉴스 검색 → 관측 기간 안의 기사만 → 재게재 합치기
(같은 주소·거의 같은 제목) → 기사 한 건을 문서 한 건(MD)으로. 문서 머리말: 문서 종류=뉴스·게재일·매체·원문·
검색어·재게재 수·같이 실은 매체. 이미 가진 기사(같은 원문 주소)는 다시 만들지 않는다.
재게재 수·매체 수는 화제 점수(buzz_score)가 쓴다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("agent_town.tools")

CONFIG = Path(__file__).resolve().parents[2] / "data" / "config" / "news_collect.json"
_WS = re.compile(r"[^0-9a-z]+")


def _config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _norm_url(url: str) -> str:
    p = urlparse(url)
    return f"{_domain(url)}{p.path.rstrip('/')}".lower()


def _title_key(title: str) -> str:
    """재게재 판정용 — 글자·숫자만 소문자로, 앞 70자(매체가 제목 뒤에 붙이는 ' - Reuters' 등은 잘라 냄)."""
    t = re.split(r"\s[-|–—]\s[^-|–—]{2,40}$", title or "")[0]
    return _WS.sub("", t.lower())[:70]


def _pub_date(r: dict) -> str | None:
    raw = r.get("published_date") or ""
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:len(raw)], fmt).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
    return m.group(1) if m else None


def _search(query: str, start: date, end: date, cfg: dict) -> list[dict]:
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        raise RuntimeError("TAVILY_API_KEY 가 .env 에 없습니다.")
    body = {"query": query, "topic": "news", "search_depth": cfg.get("search_depth", "basic"),
            "max_results": int(cfg.get("max_results_per_query", 20)), "include_answer": False,
            "start_date": start.isoformat(), "end_date": end.isoformat()}   # days 와 함께 보내면 Tavily 가 400 으로 거부한다
    if cfg.get("exclude_domains"):
        body["exclude_domains"] = cfg["exclude_domains"]
    base = os.getenv("TAVILY_BASE_URL") or "https://api.tavily.com"
    req = urllib.request.Request(f"{base}/search", data=json.dumps(body).encode("utf-8"),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read()).get("results") or []


def collect(args: dict, known_urls: set[str]) -> dict:
    """{"ok", "documents": [{"filename","content","url","published"}], "notes"}. known_urls = 이미 가진 기사(정규화 주소)."""
    cfg = _config()
    end = date.fromisoformat(str(args.get("window_end") or date.today().isoformat())[:10])
    start = date.fromisoformat(str(args.get("window_start") or (end - timedelta(days=6)).isoformat())[:10])
    queries = [q.strip() for q in re.split(r"[\n;]", str(args.get("queries") or "")) if q.strip()] or cfg["queries"]
    groups: dict[str, dict] = {}     # 제목 키 → 대표 기사 + 재게재
    fails, raw_n, out_of_window, undated = [], 0, 0, 0
    # 검색 한 번에 최대 20건이라, 관측 기간을 split_days 일씩 나눠 검색해 더 많이 모은다(2026-09-29: 한 주 193건 → 부족)
    step = max(1, int(cfg.get("split_days", 4)))
    segments, s = [], start
    while s <= end:
        segments.append((s, min(end, s + timedelta(days=step - 1))))
        s += timedelta(days=step)
    for q in queries:
      for seg_start, seg_end in segments:
        try:
            results = _search(q, seg_start, seg_end, cfg)
        except Exception as e:
            fails.append(f"{q} {seg_start}: {type(e).__name__}")
            continue
        time.sleep(0.2)
        for r in results:
            raw_n += 1
            url = r.get("url") or ""
            if not url:
                continue
            pub = _pub_date(r)
            if pub is None:
                undated += 1
                continue
            if not (start.isoformat() <= pub <= end.isoformat()):
                out_of_window += 1
                continue
            k = _title_key(r.get("title", "")) or _norm_url(url)
            g = groups.get(k)
            if g is None:
                groups[k] = {"title": r.get("title", "").strip(), "url": url, "domain": _domain(url), "published": pub,
                             "snippet": (r.get("content") or "").strip(), "queries": {q}, "domains": {_domain(url)}, "urls": {_norm_url(url)}}
            else:
                g["queries"].add(q)
                g["domains"].add(_domain(url))
                g["urls"].add(_norm_url(url))
                if pub < g["published"]:
                    g["published"] = pub
    docs = []
    reused = 0
    for g in groups.values():
        if g["urls"] & known_urls:
            reused += 1
            continue
        header_rows = [("문서 종류", "뉴스"), ("기간", end.isoformat()), ("게재일", g["published"]),
                       ("게재일 근거", "Tavily 뉴스 검색의 published_date"), ("매체", g["domain"]), ("원문", g["url"]),
                       ("검색어", " / ".join(sorted(g["queries"]))), ("재게재 수", str(len(g["urls"]))),
                       ("같이 실은 매체", ", ".join(sorted(g["domains"])))]
        header = "| 항목 | 값 |\n|---|---|\n" + "\n".join(f"| {k} | {v.replace('|', '/')} |" for k, v in header_rows) + "\n\n---\n\n"
        body = f"# {g['title']}\n\n{g['snippet']}\n"
        stem = re.sub(r"[^0-9A-Za-z]+", "_", g["title"])[:50].strip("_") or "article"
        docs.append({"filename": f"news_{g['published']}_{stem}.md", "content": header + body, "url": g["url"], "published": g["published"]})
    notes = [f"검색어 {len(queries)}개 · 결과 {raw_n}건 → 기간 밖 {out_of_window} · 날짜 없음 {undated} · "
             f"재게재 합쳐 기사 {len(groups)}건 · 새로 저장 {len(docs)} · 이미 있음 {reused}"]
    if fails:
        notes.append(f"검색 실패 {len(fails)}개: " + "; ".join(fails[:5]))
    return {"ok": bool(groups) or not fails, "documents": docs, "notes": notes}
