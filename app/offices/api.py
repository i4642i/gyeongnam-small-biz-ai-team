"""사무실 흐름 API: 의뢰 실행·중지·결재, 실행 기록, 부서 결과 JSON, 보고서·대시보드."""

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.offices import dashboard, final_report, flow, store

router = APIRouter(prefix="/api/offices", tags=["office-flow"])


def _office(office_id: str) -> dict:
    try:
        return store.get(office_id)
    except store.OfficeNotFound:
        raise HTTPException(404, "사무실을 찾을 수 없습니다")


def _guard(fn, *a):
    try:
        return fn(*a)
    except flow.FlowError as e:
        if e.problems:
            raise HTTPException(e.code, detail={"message": str(e), "problems": e.problems})
        raise HTTPException(e.code, str(e))


class RunBody(BaseModel):
    inputs: dict = {}
    auto_approve: bool = False   # 참이면 결재 관문을 자동 승인한다(상담소에서 의뢰한 흐름)


@router.get("/{office_id}/flow")
def get_flow(office_id: str):
    """흐름 설정과 계획(부서별 실행 방식, 앉은 책상의 업무·입력, 사무실 공통 입력, 실행을 막는 문제)."""
    office = _office(office_id)
    return {"config": flow.get_config(office), "plan": flow.build_plan(office), "running": flow.running(office_id)}


@router.post("/{office_id}/flows", status_code=202)
def run_flow(office_id: str, body: RunBody):
    _office(office_id)
    return _guard(flow.start, office_id, body.inputs, body.auto_approve)


@router.post("/{office_id}/flows/{flow_id}/stop", status_code=202)
def stop_flow(office_id: str, flow_id: str):
    """"작업 정지": 지금 도는 흐름을 멈춘다. 안 보낸 책상은 안 보내고, 도는 책상은 다음 모델 왕복을 취소한다."""
    _office(office_id)
    return _guard(flow.request_stop, office_id, flow_id)


@router.get("/{office_id}/flows/{flow_id}/desks/{desk_key}/prompt")
def desk_prompt(office_id: str, flow_id: str, desk_key: str, download: bool = False):
    """결재 대기 중인 이 책상의 전송 프롬프트 미리보기(모델에 보낼 내용, 아직 안 보냄)."""
    _office(office_id)
    text = flow.read_desk_prompt(office_id, flow_id, desk_key)
    if text is None:
        raise HTTPException(404, "이 책상의 프롬프트가 아직 준비되지 않았습니다.")
    headers = {"Content-Disposition": f'attachment; filename="{flow_id}_{desk_key.replace(":", "_")}.prompt.txt"'} if download else {}
    return Response(text, media_type="text/plain; charset=utf-8", headers=headers)


@router.post("/{office_id}/flows/{flow_id}/desks/{desk_key}/approve", status_code=202)
def approve_desk(office_id: str, flow_id: str, desk_key: str):
    """이 책상을 승인한다 — 그때 비로소 모델에 전송된다."""
    _office(office_id)
    flow.approve_desk(flow_id, desk_key)
    return {"ok": True, "approved": desk_key}


@router.post("/{office_id}/flows/{flow_id}/desks/{desk_key}/reject", status_code=202)
def reject_desk(office_id: str, flow_id: str, desk_key: str):
    """이 책상을 거부한다 — 전송하지 않고 실패로 남긴다."""
    _office(office_id)
    flow.reject_desk(flow_id, desk_key)
    return {"ok": True, "rejected": desk_key}


@router.get("/{office_id}/flows")
def list_flows(office_id: str):
    _office(office_id)
    return {"flows": flow.list_flows(office_id), "running": flow.running(office_id)}


@router.get("/{office_id}/flows/{flow_id}")
def get_flow_run(office_id: str, flow_id: str):
    _office(office_id)
    return flow.detail(_guard(flow.get, office_id, flow_id))


@router.get("/{office_id}/flows/{flow_id}/desks/{desk_key}/json")
def desk_json(office_id: str, flow_id: str, desk_key: str, download: bool = False):
    """한 책상이 다음 부서로 넘긴 결과 JSON(검증을 통과한 것만 저장돼 있다)."""
    _office(office_id)
    f = _guard(flow.get, office_id, flow_id)
    if desk_key not in (f.get("outputs") or {}):
        raise HTTPException(404, "이 책상의 결과 JSON 이 없습니다(실패했거나 아직 끝나지 않았습니다)")
    headers = {"Content-Disposition": f'attachment; filename="{flow_id}_{desk_key.replace(":", "_")}.json"'} if download else {}
    return Response(json.dumps(f["outputs"][desk_key], ensure_ascii=False, indent=2), media_type="application/json; charset=utf-8", headers=headers)


@router.get("/{office_id}/flows/{flow_id}/report")
def flow_report(office_id: str, flow_id: str, download: bool = False):
    """4부: 이 흐름의 최종 보고서(운용팀 결과까지 조립한 한 장짜리 MD). 운용팀 결과가 없으면 404."""
    _office(office_id)
    f = _guard(flow.get, office_id, flow_id)
    md = f.get("final_report_md") or final_report.build(f)   # 저장돼 있으면 그대로, 없으면(옛 실행 기록) 그 자리에서 조립
    if not md:
        raise HTTPException(404, "이 흐름에는 아직(또는 끝내) 최종 보고서가 없습니다(운용팀 결과가 없습니다).")
    headers = {"Content-Disposition": f'attachment; filename="{flow_id}_final_report.md"'} if download else {}
    return Response(md, media_type="text/markdown; charset=utf-8", headers=headers)


@router.get("/{office_id}/flows/{flow_id}/report/readable")
def flow_report_readable(office_id: str, flow_id: str):
    """회사 최종 보고서의 읽기 전용 HTML(2026-09-29) — 같은 MD 를 표·제목이 보이게 바꿔 보여 준다(모델 호출 없음)."""
    from app.runs import readable
    _office(office_id)
    f = _guard(flow.get, office_id, flow_id)
    md = f.get("final_report_md") or final_report.build(f)
    if not md:
        raise HTTPException(404, "이 흐름에는 아직(또는 끝내) 최종 보고서가 없습니다(운용팀 결과가 없습니다).")
    return Response(readable.render_markdown(md, f"회사 최종 보고서 · {f.get('office_name', '')}"), media_type="text/html; charset=utf-8")


@router.get("/{office_id}/reports")
def list_reports(office_id: str):
    """지난 보고서 목록(대시보드의 드롭다운): 끝까지 성공한 실행을 최신순으로. 지역·구역·업종을 사람이 읽는 글로 준다."""
    from app import regions
    _office(office_id)
    out = []
    for s in flow.list_flows(office_id, 60):
        if s.get("status") != "ok":
            continue
        inp = s.get("inputs") or {}
        _rid, rc = regions.of_inputs(inp)
        item = {"flow_id": s["id"], "finished_at": s.get("finished_at"), "region": rc.get("region_name", ""),
                "scope": regions.scope_text(rc, inp.get("area"), inp.get("upjong")),
                "window_start": inp.get("window_start"), "window_end": inp.get("window_end")}
        try:   # 포스터에 보일 한 줄 요약·동네 수·등급 분포(실행 기록에서 읽는다)
            full = flow.get(office_id, s["id"])
            outs = (full.get("outputs") or {}).values()
            picks = next((v for v in outs if isinstance(v, dict) and v.get("report") == "changwon_picks"), None) or {}
            health = [v for v in (full.get("outputs") or {}).values() if isinstance(v, dict) and v.get("report") == "changwon_health"]
            grades = {}
            for h in health:
                g = h.get("grade") or "?"
                grades[g] = grades.get(g, 0) + 1
            item.update({"headline": picks.get("headline", ""), "areas": len(picks.get("picks") or []), "grades": grades})
        except Exception:
            pass
        out.append(item)
    return {"reports": out}


@router.get("/{office_id}/dashboard")
def latest_dashboard(office_id: str, region: str | None = None, flow_id: str | None = None, v: str | None = None):
    """가장 최근에 대시보드를 만들 수 있는 실행(편집장 결과가 있는 것)의 대시보드. region 을 주면 그 지역을 의뢰한 실행 중에서."""
    from app import regions
    _office(office_id)
    hit = regions.find(region) if region else None
    if flow_id:
        html = dashboard.build({**_guard(flow.get, office_id, flow_id), "office_id": office_id}, theme=v or "")
        if html:
            return Response(html, media_type="text/html; charset=utf-8")
    for s in flow.list_flows(office_id, 60):
        if hit and regions.of_inputs(s.get("inputs"))[0] != hit[0]:
            continue
        html = dashboard.build({**_guard(flow.get, office_id, s["id"]), "office_id": office_id}, theme=v or "")
        if html:
            return Response(html, media_type="text/html; charset=utf-8")
    empty = ('<!doctype html><meta charset="utf-8"><body style="font-family:sans-serif;padding:48px;color:#555">'
             '<h2>아직 대시보드가 없습니다</h2><p>사무실 화면에서 회사를 한 번 실행하면 결과가 여기에 나타납니다.</p>')
    return Response(empty, media_type="text/html; charset=utf-8")


@router.get("/{office_id}/flows/{flow_id}/dashboard")
def flow_dashboard(office_id: str, flow_id: str):
    """회사 대시보드(2026-09-30) — 편집장 결과를 카드 + 지도 화면으로 조립한다(모델 호출 없음)."""
    _office(office_id)
    f = _guard(flow.get, office_id, flow_id)
    html = dashboard.build({**f, "office_id": office_id})
    if not html:
        raise HTTPException(404, "이 흐름에는 아직 대시보드로 만들 편집장 결과가 없습니다.")
    return Response(html, media_type="text/html; charset=utf-8")


@router.delete("/{office_id}/flows/{flow_id}")
def delete_flow_run(office_id: str, flow_id: str):
    _office(office_id)
    _guard(flow.delete, office_id, flow_id)
    return {"ok": True}
