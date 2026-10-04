"""지역 상권 화제 점수(2026-09-29, 창원에서 시작) — 뉴스 수집 컴퓨터의 기사로 화제 동네·업종을 센다.

기업 이름 대신 "동네 이름"을 찾고, 업종은 기사 본문에서 흔한 업종 키워드로 함께 태그한다(종목처럼 정확한
코드가 없는 영역이라 이름 매칭 결과를 그대로 믿지 않고, 화제 탐지 담당자가 기사를 읽고 확인하는 것을 전제로 한다).
"""

from __future__ import annotations

import json
import math
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEGACY_CONFIG = ROOT / "data" / "config" / "changwon_areas.json"
REGIONS_DIR = ROOT / "data" / "config" / "regions"          # 2026-09-29 일반화: 지역마다 data/config/regions/<region_id>.json
BUZZ_CONFIG = ROOT / "data" / "config" / "buzz_kr.json"      # 매체 등급은 국내 뉴스 회사와 공유
REPS = 2
UPJONG = ["카페", "음식점", "식당", "술집", "주점", "베이커리", "편의점", "미용실", "헬스장", "요가", "필라테스",
          "학원", "독서실", "스터디카페", "부동산", "옷가게", "네일샵", "PC방", "당구장", "노래방", "약국", "병원", "치과"]


# 기사 파일 읽기·결과 크기 맞추기(옛 buzz.py 에서 옮겨 옴, 2026-10-03)
def parse_article(text: str) -> dict:
    head, _, body = text.partition("\n---\n")
    rows = dict(re.findall(r"^\|\s*([^|]+?)\s*\|\s*(.*?)\s*\|$", head, flags=re.M))
    m = re.search(r"^#\s+(.+)$", body, flags=re.M)
    title = m.group(1).strip() if m else ""
    title = re.sub(r"\s[-|–—]\s[\w.\- ]{2,40}$", "", title)
    return {"title": title, "body": body, "published": rows.get("게재일", ""), "domain": rows.get("매체", ""),
            "url": rows.get("원문", ""), "reprints": int(rows.get("재게재 수") or 1),
            "domains": [d.strip() for d in (rows.get("같이 실은 매체") or "").split(",") if d.strip()]}


MAX_JSON_CHARS = 19000   # 도구 결과 한도(20000, 2026-09-29) 안에 항상 들어오게 — 넘치면 하위권부터 뺀다(잘린 JSON을 만들지 않는다)


def _fit(ranked: list[dict], top: int, used: int, no_ticker: int, window: list[str]) -> dict:
    available = min(top, len(ranked))   # 크기 때문이 아니라 후보 자체가 top 보다 적을 수 있음 — 그 경우와 구분한다
    n = available
    while n > 0:
        out = {"ok": True, "window": window, "articles_used": used, "articles_without_ticker": no_ticker,
               "tickers_found": len(ranked), "ranked": ranked[:n],
               "notes": [f"기사 {used}건 중 종목을 못 찾은 기사 {no_ticker}건 · 종목 {len(ranked)}개 · 상위 {n}개" +
                        (f"(크기 한도로 {available}개 중 {n}개만 담음)" if n < available else "")]}
        if len(json.dumps(out, ensure_ascii=False)) <= MAX_JSON_CHARS:
            return out
        n -= 1
    return {"ok": True, "window": window, "articles_used": used, "articles_without_ticker": no_ticker,
            "tickers_found": len(ranked), "ranked": [], "notes": ["종목이 없거나 결과가 너무 큽니다."]}


def _config(region_id: str = "changwon") -> dict:
    """region_id 를 안 주면 창원(기존 회사와 호환)."""
    p = REGIONS_DIR / f"{region_id}.json"
    if not p.is_file() and region_id == "changwon" and LEGACY_CONFIG.is_file():
        p = LEGACY_CONFIG
    if not p.is_file():
        raise FileNotFoundError(f"지역 설정을 찾을 수 없습니다: {region_id} ({p})")
    return json.loads(p.read_text(encoding="utf-8"))


def _tier(domain: str, cfg: dict) -> float:
    for level, doms in (cfg.get("domain_tier") or {}).items():
        for d in doms:
            if domain == d or domain.endswith("." + d):
                return float(level)
    return float(cfg.get("default_tier", 0.5))


class AreaResolver:
    def __init__(self, cfg: dict, keep: set | None = None, extra: set | None = None):
        """keep: 법정동 별칭 중 소속 행정동으로 합치지 않고 따로 후보로 둘 이름(동 단위 의뢰에서 그 동 안의 법정동)."""
        self.district_of: dict[str, str] = {}
        # 구가 하나뿐인 시·군은 그 이름(예: 진주시)이 모든 기사에 나와 동네 후보가 될 수 없으므로 뺀다
        names = ([d["name"] for d in cfg["districts"]] if len(cfg["districts"]) > 1 else []) + [a["name"] for a in cfg["known_areas"]]
        for a in cfg["known_areas"]:
            self.district_of[a["name"]] = a["district"]
        for a, alist in (cfg.get("aliases") or {}).items():
            names += alist
        # 법정동 이름만 나온 기사도 소속 행정동으로 잡는다(2026-09-30, 상가 데이터의 법정동→행정동 대응)
        keep = keep or set()
        alias = cfg.get("area_aliases") or {}
        self.canon = {k: v for k, v in alias.items() if k not in keep}
        names += list(alias.keys())
        names += [n for n in (extra or set())]   # 일반 낱말과 겹쳐 자동 후보에서 뺀 동도, 그 동을 의뢰했다면 찾는다
        for k in keep:
            if k in alias:
                self.district_of[k] = self.district_of.get(alias[k], "")
        alts = sorted(set(names), key=len, reverse=True)
        self._re = re.compile("(" + "|".join(re.escape(n) for n in alts) + ")") if alts else None
        self._upjong_re = re.compile("(" + "|".join(UPJONG) + ")")

    def find_areas(self, text: str) -> set[str]:
        return {self.canon.get(m, m) for m in self._re.findall(text)} if self._re else set()

    def find_upjong(self, text: str) -> set[str]:
        return set(self._upjong_re.findall(text))


def score(articles: list[dict], start: str, end: str, top: int = 30, cfg: dict | None = None, region_id: str = "changwon",
          scope_area: str | None = None, upjong: str | None = None) -> dict:
    from app import regions
    area_cfg = _config(region_id)
    buzz_cfg = json.loads(BUZZ_CONFIG.read_text(encoding="utf-8")) if cfg is None else cfg
    scope = regions.resolve_scope(area_cfg, scope_area)   # 시 전체 · 구 · 동(의뢰 구역)
    allowed = regions.scope_area_names(area_cfg, scope)   # None 이면 제한 없음
    res = AreaResolver(area_cfg, keep=set(scope["ldongs"]) if scope["kind"] == "dong" else None,
                       extra={scope["dong"]} if scope["kind"] == "dong" else None)
    if scope["kind"] == "dong":
        res.district_of.setdefault(scope["dong"], scope["district"])
    up_terms = [t for t in re.split(r"[/·,\s]+", str(upjong or "")) if len(t) >= 2]   # 의뢰 업종을 말하는 기사는 조금 더 무겁게
    w = buzz_cfg.get("weights", {})
    in_window = [a for a in articles if start <= a["published"] <= end]
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    span = max(1, (d1 - d0).days)
    mid = d0.toordinal() + span / 2
    floor = float(buzz_cfg.get("recency_floor", 0.6))
    boost = float(buzz_cfg.get("title_boost", 1.5))
    agg: dict[str, dict] = {}
    used = no_area = 0
    for a in in_window:
        used += 1
        in_title = res.find_areas(a["title"])
        areas = in_title | res.find_areas(a["body"])
        if allowed is not None:   # 의뢰 구역 밖의 동네는 집계 전에 뺀다
            in_title, areas = in_title & allowed, areas & allowed
        if not areas:
            no_area += 1
            continue
        upjong_tags = res.find_upjong(a["title"] + " " + a["body"])
        day = date.fromisoformat(a["published"]).toordinal()
        recency = floor + (1 - floor) * (day - d0.toordinal()) / span
        tier = _tier(a["domain"], buzz_cfg)
        share = 1 / math.sqrt(len(areas))
        for area in areas:
            g = agg.setdefault(area, {"mention": 0.0, "n": 0, "title_n": 0, "domains": set(), "early": 0, "late": 0,
                                      "arts": [], "upjong": {}})
            boost_up = 1.4 if up_terms and any(t in a["title"] + a["body"] for t in up_terms) else 1
            g["mention"] += tier * (boost if area in in_title else 1) * recency * share * boost_up
            g["n"] += 1
            g["title_n"] += area in in_title
            g["domains"].update(a["domains"] or [a["domain"]])
            g["late" if day >= mid else "early"] += 1
            g["arts"].append((area in in_title, tier, a["published"], a))
            for u in upjong_tags:
                g["upjong"][u] = g["upjong"].get(u, 0) + 1
    ranked = []
    for area, g in agg.items():
        momentum = (g["late"] - g["early"]) / (g["late"] + g["early"])
        s = (float(w.get("mentions", 1)) * math.log1p(g["mention"]) + float(w.get("diversity", 0.8)) * math.log1p(len(g["domains"]))
             + float(w.get("momentum", 0.5)) * momentum)
        reps = sorted(g["arts"], key=lambda x: (x[0], x[1], x[2]), reverse=True)[:REPS]
        top_upjong = sorted(g["upjong"].items(), key=lambda x: -x[1])[:3]
        ranked.append({"area": area, "district": res.district_of.get(area, ""), "score": round(s, 3),
                       "articles": g["n"], "in_title": g["title_n"], "domains": len(g["domains"]),
                       "early": g["early"], "late": g["late"], "mentioned_upjong": [u for u, _ in top_upjong],
                       "representative": [{"title": r[3]["title"][:110], "domain": r[3]["domain"], "published": r[3]["published"],
                                           "url": r[3]["url"]} for r in reps]})
    ranked.sort(key=lambda r: (-r["score"], r["area"]))
    fitted = _fit(ranked, top, used, no_area, [start, end])
    fitted["areas_without_match"] = fitted.pop("articles_without_ticker")
    fitted["areas_found"] = fitted.pop("tickers_found")
    fitted["notes"] = [n.replace("종목을 못 찾은 기사", "동네를 못 찾은 기사").replace("종목", "동네") for n in fitted["notes"]]
    return fitted


def score_library(library_id: str, start: str, end: str, top: int = 30, region_id: str = "changwon",
                  area: str | None = None, upjong: str | None = None) -> dict:
    from app.rag import store as rag_store
    d = rag_store.docs_dir(library_id)
    arts = []
    for doc in rag_store.list_documents(library_id):
        p = d / doc["stored_name"]
        if p.suffix == ".md" and p.exists():
            arts.append(parse_article(p.read_text(encoding="utf-8")))
    return score(arts, start, end, top, region_id=region_id, scope_area=area, upjong=upjong)
