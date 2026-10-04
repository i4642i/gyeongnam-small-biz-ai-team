"""국내 뉴스 수집(2026-09-29) — 회사 컴퓨터 도구 naver_news. 모델을 부르지 않는다. app/tools/newscollect.py(미국판)의 국내 버전.

검색어 묶음(data/config/naver_news.json)마다 네이버 뉴스 검색 API를 최신순으로 훑다가 관측 시작일보다 오래된
기사가 나오면 그 검색어는 멈춘다(이 API는 기간 지정이 안 됨). 관측 기간 안의 기사만 → 재게재 합치기(같은 주소·
거의 같은 제목) → 기사 한 건을 문서 한 건(MD)으로. 문서 머리말은 미국판과 같은 항목 이름을 쓴다(화제 점수 도구가
그대로 읽을 수 있게: 문서 종류·기간·게재일·매체·원문·검색어·재게재 수·같이 실은 매체).
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import time
import urllib.request
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("agent_town.tools")

CONFIG = Path(__file__).resolve().parents[2] / "data" / "config" / "naver_news.json"
_WS = re.compile(r"[^0-9a-z가-힣]+")
_TAG = re.compile(r"</?b>")


def _config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _norm_url(url: str) -> str:
    p = urlparse(url)
    return f"{_domain(url)}{p.path.rstrip('/')}".lower()


def _title_key(title: str) -> str:
    t = re.split(r"\s[-|–—]\s[^-|–—]{2,40}$", title or "")[0]
    return _WS.sub("", t.lower())[:70]


def _unescape(s: str) -> str:
    return html.unescape(_TAG.sub("", s or "")).strip()


def _pub_date(raw: str) -> str | None:
    try:
        return parsedate_to_datetime(raw).date().isoformat()
    except (TypeError, ValueError):
        return None


def _search(query: str, start: int, display: int) -> list[dict]:
    """네이버 검색 API 인증은 발급 경로에 따라 둘로 갈린다(2026-09-29, 실제 발급해 보고 확인):
    - developers.naver.com 개인 오픈API: X-Naver-Client-Id/Secret, openapi.naver.com
    - NAVER Cloud Platform(NCP) API HUB: X-NCP-APIGW-API-KEY-ID/KEY, naveropenapi.apigw.ntruss.com
    두 방식 다 같은 환경변수 이름(NAVER_CLIENT_ID/NAVER_CLIENT_SECRET)을 쓰고, NCP 쪽인지는 NAVER_AUTH=ncp 로 알려준다."""
    cid, secret = os.getenv("NAVER_CLIENT_ID"), os.getenv("NAVER_CLIENT_SECRET")
    if not (cid and secret):
        raise RuntimeError("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 가 .env 에 없습니다.")
    from urllib.parse import urlencode
    query_s = urlencode({"query": query, "display": display, "start": start, "sort": "date"})
    if (os.getenv("NAVER_AUTH") or "").strip().lower() == "ncp":
        url = "https://naverapihub.apigw.ntruss.com/search/v1/news?" + query_s   # 실측(2026-09-29): 도메인 naverapihub(≠naveropenapi), .json 없음
        headers = {"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": secret}
    else:
        url = "https://openapi.naver.com/v1/search/news.json?" + query_s
        headers = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("items") or []


BODY_MAX = 3000        # 문서에 싣는 본문 길이(앞부분). 저작권·검색 조각 수를 생각해 앞 3,000자만.
BODY_MIN = 200         # 이보다 짧으면 본문 읽기에 실패한 것으로 보고 미리보기만 쓴다
BODY_TIMEOUT = 10
BODY_WORKERS = 6


_NAV_STOP = re.compile(r"(무단\s*전재|저작권자|Copyright|ⓒ|©|기사제보|구독신청|관련\s*기사|많이\s*본|댓글|공유하기|이메일\s*[:：]|기자의\s*다른)")


def _main_text(text: str) -> str:
    """페이지 전체 글에서 기사 본문만 추린다. 메뉴·광고·버튼 줄은 짧아서, 한 줄이 30자 이상인 줄만 본문으로 본다.
    저작권·관련 기사 같은 끝맺음 표시가 나오면 거기서 멈춘다."""
    out = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t　]+", " ", raw).strip()
        if not line:
            continue
        if sum(map(len, out)) >= 500 and _NAV_STOP.search(line) and len(line) < 120:   # 머리쪽 글자(기자 이메일·공유 버튼)에서 멈추지 않게 본문이 어느 정도 쌓인 뒤에만
            break
        if len(line) >= 30 and line not in out:   # 같은 줄(제목이 두세 번 나오는 페이지)은 한 번만
            out.append(line)
    return "\n\n".join(out)


def _article_body(url: str) -> str:
    """기사 원문 주소에서 본문 앞부분을 읽는다(2026-10-01). 실패하면 빈 문자열 — 호출한 쪽이 미리보기만 저장한다.
    pagefetch 의 안전 검사(공인 주소만, 이동 검사, 1MB 한도)를 그대로 쓴다."""
    try:
        from app.tools import pagefetch
        raw, ctype, charset, final = pagefetch._get(url, BODY_TIMEOUT)
        if ctype not in ("text/html", "application/xhtml+xml"):
            return ""
        body = (pagefetch.analyze(pagefetch._decode(raw, charset), final).get("body") or "").strip()
    except Exception:
        return ""
    body = _main_text(body)
    if len(body) < BODY_MIN:
        return ""
    if len(body) > BODY_MAX:
        cut = body[:BODY_MAX]
        k = max(cut.rfind("다."), cut.rfind(". "), cut.rfind("\n"))   # 문장 끝에서 자른다
        body = cut[: k + 1 if k > BODY_MAX * 0.6 else BODY_MAX].rstrip() + " …"
    return body


def collect(args: dict, known_urls: set[str]) -> dict:
    """{"ok", "documents": [{"filename","content","url","published"}], "notes"}. known_urls = 이미 가진 기사(정규화 주소)."""
    cfg = _config()
    end = date.fromisoformat(str(args.get("window_end") or date.today().isoformat())[:10])
    start = date.fromisoformat(str(args.get("window_start") or (end - timedelta(days=6)).isoformat())[:10])
    queries = [q.strip() for q in re.split(r"[\n;]", str(args.get("queries") or "")) if q.strip()] or cfg["queries"]
    excl = set(cfg.get("exclude_domains") or [])
    display, max_pages = int(cfg.get("display", 100)), int(cfg.get("max_pages", 8))
    groups: dict[str, dict] = {}
    fails, raw_n, out_of_window, undated, excluded_n = [], 0, 0, 0, 0
    for q in queries:
        page_start = 1
        for _ in range(max_pages):
            try:
                items = _search(q, page_start, display)
            except Exception as e:
                fails.append(f"{q} p{page_start}: {type(e).__name__}")
                break
            time.sleep(0.15)
            if not items:
                break
            stop = False
            for r in items:
                raw_n += 1
                url = r.get("originallink") or r.get("link") or ""
                if not url:
                    continue
                if _domain(url) in excl:
                    excluded_n += 1
                    continue
                pub = _pub_date(r.get("pubDate") or "")
                if pub is None:
                    undated += 1
                    continue
                if pub < start.isoformat():
                    stop = True   # 최신순 정렬이라 이 검색어는 더 봐도 다 오래된 기사
                    continue
                if pub > end.isoformat():
                    out_of_window += 1
                    continue
                title = _unescape(r.get("title", ""))
                k = _title_key(title) or _norm_url(url)
                g = groups.get(k)
                if g is None:
                    groups[k] = {"title": title, "url": url, "domain": _domain(url), "published": pub,
                                "snippet": _unescape(r.get("description", "")), "queries": {q},
                                "domains": {_domain(url)}, "urls": {_norm_url(url)}}
                else:
                    g["queries"].add(q)
                    g["domains"].add(_domain(url))
                    g["urls"].add(_norm_url(url))
                    if pub < g["published"]:
                        g["published"] = pub
            if stop or len(items) < display:
                break
            page_start += display
    docs = []
    reused = 0
    fresh = []
    for g in groups.values():
        if g["urls"] & known_urls:
            reused += 1
            continue
        fresh.append(g)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=BODY_WORKERS) as ex:   # 새 기사만 본문을 읽는다(이미 가진 기사는 다시 읽지 않는다)
        bodies = list(ex.map(lambda g: _article_body(g["url"]), fresh))
    body_ok = 0
    for g, art_body in zip(fresh, bodies):
        body_ok += bool(art_body)
        header_rows = [("문서 종류", "뉴스"), ("기간", end.isoformat()), ("게재일", g["published"]),
                       ("게재일 근거", "네이버 뉴스 검색의 pubDate"), ("매체", g["domain"]), ("원문", g["url"]),
                       ("검색어", " / ".join(sorted(g["queries"]))), ("재게재 수", str(len(g["urls"]))),
                       ("같이 실은 매체", ", ".join(sorted(g["domains"]))),
                       ("본문", "원문 앞부분 수록" if art_body else "미리보기만(본문 읽기 실패)")]
        header = "| 항목 | 값 |\n|---|---|\n" + "\n".join(f"| {k} | {v.replace('|', '/')} |" for k, v in header_rows) + "\n\n---\n\n"
        body = f"# {g['title']}\n\n{g['snippet']}\n" + (f"\n## 본문(앞부분)\n\n{art_body}\n" if art_body else "")
        stem = re.sub(r"[^0-9A-Za-z가-힣]+", "_", g["title"])[:50].strip("_") or "article"
        docs.append({"filename": f"news_{g['published']}_{stem}.md", "content": header + body, "url": g["url"], "published": g["published"]})
    notes = [f"검색어 {len(queries)}개 · 결과 {raw_n}건 → 기간 밖 {out_of_window} · 날짜 없음 {undated} · 제외 매체 {excluded_n} · "
             f"재게재 합쳐 기사 {len(groups)}건 · 새로 저장 {len(docs)} (본문 포함 {body_ok} · 미리보기만 {len(docs) - body_ok}) · 이미 있음 {reused}"]
    if fails:
        notes.append(f"검색 실패 {len(fails)}개: " + "; ".join(fails[:5]))
    return {"ok": bool(groups) or not fails, "documents": docs, "notes": notes}
