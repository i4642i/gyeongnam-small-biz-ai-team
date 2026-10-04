"""의뢰 지역(2026-09-30): data/config/regions/<id>.json 을 읽는 공통 자리.

회사(직원 4명)는 하나이고, 실행할 때 입력값 `region`(예: "진주시")으로 지역을 정한다. 도구·검증·대시보드가 모두 여기서 그 지역 설정을 찾는다.
지역 설정 파일은 tools/build_region_config.py 로 만들고(창원은 손으로 만든 첫 지역), `news_computer` 는 그 지역 전용 뉴스 컴퓨터 id 다.
"""

from __future__ import annotations

import json
from pathlib import Path

REGIONS_DIR = Path(__file__).resolve().parents[1] / "data" / "config" / "regions"
DEFAULT_ID = "changwon"   # 옛 실행 기록·지역 입력이 없는 실행은 창원


def all_regions() -> dict[str, dict]:
    out = {}
    for p in sorted(REGIONS_DIR.glob("*.json")):
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out[p.stem] = cfg
    return out


def find(value) -> tuple[str, dict] | None:
    """지역 id·이름(창원시)·짧은 이름(창원)으로 (id, 설정)을 찾는다."""
    v = str(value or "").strip()
    if not v:
        return None
    for rid, cfg in all_regions().items():
        if v in (rid, cfg.get("region_name"), cfg.get("label")):
            return rid, cfg
    return None


def of_inputs(inputs: dict | None) -> tuple[str, dict]:
    """실행 입력값의 region → (id, 설정). 없거나 모르는 값이면 창원."""
    hit = find((inputs or {}).get("region"))
    if hit:
        return hit
    cfg = all_regions().get(DEFAULT_ID) or {}
    return DEFAULT_ID, cfg


def district_names() -> set[str]:
    """모든 지역의 구·시 이름(검증 훅이 '이 나라에 있는 이름인가'를 볼 때 쓴다)."""
    return {d["name"] for cfg in all_regions().values() for d in cfg.get("districts", [])}


# ---------------------------------------------------------------- 의뢰 구역(2026-09-30): 시 전체 · 구 · 행정동, 그리고 업종

def tree_districts(cfg: dict) -> list[dict]:
    return (cfg.get("tree") or {}).get("districts", [])


def resolve_scope(cfg: dict, area) -> dict:
    """의뢰 구역 문자열 → {"kind": city|district|dong, "district", "dong", "ldongs", "label", "found"}.
    형식: 비어 있음(시 전체) · "성산구"(구) · "성산구 상남동"(구 + 동) · "평거동"(구가 없는 시의 동). 모르는 이름은 시 전체로 돌린다(found=False)."""
    a = str(area or "").strip()
    ds = tree_districts(cfg)
    city = {"kind": "city", "district": None, "dong": None, "ldongs": [], "label": cfg.get("region_name", ""), "found": True}
    if not a:
        return city
    parts = a.split()
    multi = len(ds) > 1
    if len(parts) == 2:
        want_d, want_a = parts
    else:
        want_d, want_a = None, parts[0]
        d = next((d for d in ds if multi and d["name"] == want_a), None)
        if d:
            return {"kind": "district", "district": d["name"], "dong": None, "ldongs": [], "label": d["name"], "found": True}
    for d in ds:
        if want_d and d["name"] != want_d:
            continue
        for ar in d["areas"]:
            if ar["name"] == want_a:
                label = f"{d['name']} {ar['name']}" if multi else ar["name"]
                return {"kind": "dong", "district": d["name"], "dong": ar["name"], "ldongs": [x for x in ar["ldongs"] if x != ar["name"]],
                        "label": label, "found": True}
    return {**city, "found": False}


def scope_area_names(cfg: dict, scope: dict) -> set | None:
    """화제 점수에서 이 구역 안의 동네로 인정할 이름 집합(시 전체는 None = 제한 없음)."""
    if scope["kind"] == "city":
        return None
    if scope["kind"] == "district":
        return {k["name"] for k in cfg.get("known_areas", []) if k.get("district") == scope["district"]} | {scope["district"]}
    names = {scope["dong"]} | set(scope["ldongs"])
    names |= {k["name"] for k in cfg.get("known_areas", []) if k["name"] == scope["dong"] or k["name"].startswith(scope["dong"] + "(")}
    return names


def scope_text(cfg: dict, area, upjong) -> str:
    sc = resolve_scope(cfg, area)
    parts = [cfg.get("region_name", "")]
    if sc["kind"] != "city":
        parts.append(sc["label"])
    if str(upjong or "").strip():
        parts.append(str(upjong).strip())
    return " · ".join(p for p in parts if p)


def news_queries(cfg: dict, area, upjong) -> list[str]:
    """의뢰 구역·업종에 맞춘 뉴스 검색어. 시 전체는 설정의 기본 검색어, 구·동은 그 이름으로 좁히고 업종이 있으면 업종 검색어를 더한다."""
    sc, label, up = resolve_scope(cfg, area), cfg.get("label", ""), str(upjong or "").strip()
    generic = ["상권", "상가", "개업 폐업", "재개발", "축제", "임대료"]
    if sc["kind"] == "city":
        qs = list(cfg.get("news_queries") or [])
    elif sc["kind"] == "district":
        ds = next((d for d in tree_districts(cfg) if d["name"] == sc["district"]), {"areas": []})
        qs = [f"{sc['district']} {g}" for g in generic] + [f"{label} {a['name']}" for a in ds["areas"][:6]]
    else:
        qs = [f"{label} {sc['dong']} {g}" for g in generic] + [f"{label} {n}" for n in sc["ldongs"][:4]]
    if up:
        base = sc["dong"] or sc["district"] or label
        qs += [f"{base} {up} {t}" for t in ("창업", "개업", "폐업", "상권")]
    return list(dict.fromkeys(q for q in qs if q.strip()))
