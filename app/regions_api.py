"""의뢰 지역 목록(2026-09-30): 상담소 화면이 지도에 핀을 찍고, 지역을 누르면 그 지역으로 회사를 실행한다."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app import regions
from app.offices import flow, store

router = APIRouter(prefix="/api/regions", tags=["regions"])


def _center(cfg: dict):
    centers = list(((cfg.get("coords") or {}).get("district_centers") or {}).values())
    if not centers:
        return None
    return [sum(c[0] for c in centers) / len(centers), sum(c[1] for c in centers) / len(centers)]


@router.get("")
def list_regions():
    """의뢰할 수 있는 지역과, 지역마다 가장 최근에 끝난 실행(대시보드로 바로 볼 수 있는 것)."""
    office = next(iter(store.list_all()), None)
    latest: dict[str, dict] = {}
    runs: dict[str, list] = {}
    running = None
    if office:
        running = flow.running(office["id"])
        for s in flow.list_flows(office["id"], 60):   # 최신순
            rid, rc = regions.of_inputs(s.get("inputs"))
            if s.get("status") == "ok":
                inp = s.get("inputs") or {}
                item = {"flow_id": s["id"], "finished_at": s.get("finished_at"), "scope": regions.scope_text(rc, inp.get("area"), inp.get("upjong"))}
                latest.setdefault(rid, item)
                if len(runs.setdefault(rid, [])) < 8:
                    runs[rid].append(item)
    out = []
    for rid, cfg in regions.all_regions().items():
        out.append({"id": rid, "name": cfg.get("region_name"), "label": cfg.get("label"), "roman": cfg.get("roman"), "center": _center(cfg),
                    "districts": [d["name"] for d in cfg.get("districts", [])], "areas": len(cfg.get("known_areas", [])),
                    "ready": bool(cfg.get("news_computer")), "latest": latest.get(rid), "runs": runs.get(rid, [])})
    return {"office_id": office["id"] if office else None, "running": running, "regions": out}


@router.get("/{region_id}/tree")
def region_tree(region_id: str):
    """상담소의 2단계 선택이 읽는다: 시(구) → 행정동(점포 수·자료 적음 표시), 업종 대분류 → 중분류."""
    hit = regions.find(region_id)
    if not hit:
        raise HTTPException(404, "모르는 지역입니다")
    rid, cfg = hit
    multi = len(regions.tree_districts(cfg)) > 1
    return {"id": rid, "name": cfg.get("region_name"), "label": cfg.get("label"), "multi_district": multi,
            "districts": [{"name": d["name"], "count": d["count"],
                           "areas": [{"name": a["name"], "count": a["count"], "thin": a["thin"], "lat": a.get("lat"), "lon": a.get("lon")} for a in d["areas"]]} for d in regions.tree_districts(cfg)],
            "upjong": [{"name": u["name"], "count": u["count"], "middle": [{"name": m["name"], "count": m["count"]} for m in u["middle"][:12]]}
                       for u in (cfg.get("upjong") or {}).get("large", [])]}
