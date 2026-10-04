"""상권 진단 회사 훅 공통부(2026-09-29). 뉴스회사 hooks/news_common.py 와 같은 역할."""

import json
import re
from pathlib import Path

PLATFORM_CONFIG: dict = {}
# 이 파일 위치(data/agents/<직원>/hooks/)에서 data/config/regions 를 찾는다 — 어느 PC·어느 폴더에 두어도 같다
CONFIG = Path(__file__).resolve().parents[3] / "config" / "regions" / "changwon.json"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DISTRICTS: set | None = None


def _region_cfgs() -> list:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CONFIG.parent.glob("*.json"))]


def districts() -> set:
    """의뢰 가능한 모든 지역의 구·시 이름(지역 설정 파일 전부)."""
    global _DISTRICTS
    if _DISTRICTS is None:
        _DISTRICTS = {d["name"] for cfg in _region_cfgs() for d in cfg.get("districts", [])}
    return _DISTRICTS


def single_districts() -> set:
    """구가 하나뿐인 시(진주시 등). 이런 시는 시 하나가 곧 '구'라 한 구 최대 2개 규칙을 적용하지 않는다."""
    return {cfg["districts"][0]["name"] for cfg in _region_cfgs() if len(cfg.get("districts", [])) == 1}


def phrases() -> list:
    fp = (PLATFORM_CONFIG or {}).get("forbidden_phrases") or {}
    return [p.lower() for p in (fp.get("phrases") or ["매수 추천", "투자하세요", "확실히 뜬다", "목표주가"])]


def prose(obj, skip=("key_articles", "key_article", "url", "title")) -> str:
    if isinstance(obj, dict):
        return " ".join(prose(v, skip) for k, v in obj.items() if k not in skip)
    if isinstance(obj, list):
        return " ".join(prose(v, skip) for v in obj)
    return obj if isinstance(obj, str) else ""


def forbidden(obj) -> list:
    text = prose(obj).lower()
    return [f"단정적 추천 구절 '{p}' 은(는) 쓸 수 없습니다(참고용 정보만 냅니다)." for p in phrases() if p in text]


def check_district(d, where: str) -> list:
    if not isinstance(d, str) or d not in districts():
        return [f"{where}: 구 이름 '{d}' 이 의뢰 가능한 구·시 목록에 없습니다."]
    return []


def check_article(a, start: str, end: str, where: str) -> list:
    if not isinstance(a, dict):
        return [f"{where}: 기사는 {{title, domain, published, url}} 객체여야 합니다."]
    errs = []
    pub = str(a.get("published") or "")
    if not _DATE.match(pub):
        errs.append(f"{where}: 기사 게재일 '{pub}' 은 YYYY-MM-DD 여야 합니다.")
    elif start and end and not (start <= pub <= end):
        errs.append(f"{where}: 기사 게재일 {pub} 이 관측 기간({start} ~ {end}) 밖입니다.")
    if not str(a.get("url") or "").startswith("http"):
        errs.append(f"{where}: 기사 원문 주소(url)가 없습니다.")
    return errs


def check_topics(data: dict) -> list:
    if not isinstance(data, dict):
        return ["결과 JSON 이 객체가 아닙니다."]
    errs = []
    start, end = str(data.get("window_start") or ""), str(data.get("window_end") or "")
    cands = data.get("candidates") or []
    if not (1 <= len(cands) <= 20):   # 구역이 좁은 의뢰(동 하나 등)는 후보가 적을 수 있다
        errs.append(f"candidates 는 1~20개여야 합니다(지금 {len(cands)}개).")
    for i, c in enumerate(cands):
        where = f"candidates[{i}]"
        errs += check_district((c or {}).get("district"), where)
        arts = (c or {}).get("key_articles") or []
        if not (1 <= len(arts) <= 3):
            errs.append(f"{where}: 대표 기사(key_articles)는 1~3건이어야 합니다.")
        for j, a in enumerate(arts):
            errs += check_article(a, start, end, f"{where}.key_articles[{j}]")
    return errs + forbidden(data)
