"""Server-owned retrieval scope, shared by automatic and explicit searches."""

import json
from datetime import date
from pathlib import Path

# 캐릭터별 검색 범위(2026-09-29, 뉴스 회사): {"<agent_id>": {"own": [적용할 키], "computer": [적용할 키]}}.
# 없는 캐릭터는 지금처럼 ticker·doc_type·as_of 를 모두 건다(시장조사 회사 — 미래 자료·다른 회사 자료가 섞이지 않게).
# 뉴스 회사 직원의 전문자료는 날짜·티커가 없는 '늘 쓰는 기준서'라 own=[] , 뉴스 기사에는 티커가 없어 computer=["as_of"].
SETTINGS = Path(__file__).resolve().parents[2] / "data" / "config" / "retrieval_scope.json"
KEYS = ("ticker", "doc_type", "as_of")


def allowed_keys(agent_id: str | None, target: str) -> tuple:
    if not agent_id:
        return KEYS
    try:
        cfg = json.loads(SETTINGS.read_text(encoding="utf-8")).get(agent_id)
    except (OSError, ValueError):
        cfg = None
    if not isinstance(cfg, dict) or not isinstance(cfg.get(target), list):
        return KEYS
    return tuple(k for k in cfg[target] if k in KEYS)


def iso_date(value: str) -> str:
    value = str(value).strip()
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다.")
    return value


def from_inputs(inputs: dict | None, agent_id: str | None = None, target: str = "own") -> dict:
    """target: own(캐릭터 전문자료) / computer(회사 컴퓨터). agent_id 를 주면 그 캐릭터 설정의 키만 남긴다(입력 검사는 그대로)."""
    values = inputs or {}
    candidate = values.get("candidate")
    if isinstance(candidate, str):
        candidate = json.loads(candidate)
    if candidate is not None and not isinstance(candidate, dict):
        raise ValueError("candidate 입력 형식이 올바르지 않습니다.")
    # 종목(ticker) 기반 회사만 여기서 확인한다 — 창원 상권 회사처럼 candidate 가 area·district 뿐인 회사도 있다(2026-09-29 발견).
    # ticker 가 필요한 도구(company_health 등)는 app/tools/runner.py 의 그 도구 전용 결속에서 따로 확인한다.
    if candidate and values.get("ticker") and candidate.get("ticker") and str(values["ticker"]).strip().upper() != str(candidate["ticker"]).strip().upper():
        raise ValueError("ticker와 candidate.ticker가 일치하지 않습니다.")
    out = {}
    for key in ("ticker", "doc_type"):
        value = values.get(key) or (candidate or {}).get(key)
        if value:
            out[key] = str(value).strip()
    if out.get("ticker"):
        out["ticker"] = out["ticker"].upper()
    cutoff = values.get("window_end") or values.get("as_of")
    if cutoff:
        out["as_of"] = iso_date(cutoff)
    keep = allowed_keys(agent_id, target)
    return {k: v for k, v in out.items() if k in keep}
