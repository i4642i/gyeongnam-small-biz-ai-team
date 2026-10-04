"""업무 실행 기록 API(읽기 전용): 기록 목록·상세, 결과 내려받기(마크다운 / JSON / 읽기 전용 HTML)."""

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.agents import store
from app.runs import checks, service, verify

router = APIRouter(prefix="/api", tags=["runs"])


def _agent(agent_id: str) -> dict:
    try:
        return store.get(agent_id)
    except store.AgentNotFound:
        raise HTTPException(404, "에이전트를 찾을 수 없습니다")


def _guard(fn, *a):
    try:
        return fn(*a)
    except service.RunError as e:
        raise HTTPException(e.code, str(e))


@router.get("/agents/{agent_id}/runs")
def list_runs(agent_id: str, task_id: str | None = None):
    _agent(agent_id)
    return {"runs": service.list_runs(agent_id, task_id), "running": service.is_running(agent_id)}


@router.get("/agents/{agent_id}/runs/{run_id}")
def get_run(agent_id: str, run_id: str):
    _agent(agent_id)
    run = _guard(service.get, agent_id, run_id)
    if run.get("pause"):
        run = {**run, "pause": service._pause_info(run["pause"])}   # 대화 전문(session_state)은 화면에 내보내지 않는다
    return {**run, "json_ok": checks.extract_json(run.get("answer") or "")[2] is None, "hook": verify.hook_meta(agent_id, run["task_id"])}


def _download(name: str, media: str, body: str, download: bool) -> Response:
    headers = {"Content-Disposition": f'attachment; filename="{name}"'} if download else {}
    return Response(body, media_type=media, headers=headers)


@router.get("/agents/{agent_id}/runs/{run_id}/report")
def run_report(agent_id: str, run_id: str, download: bool = False):
    """결과 전체(마크다운)."""
    _agent(agent_id)
    run = _guard(service.get, agent_id, run_id)
    if not run.get("answer"):
        raise HTTPException(404, "이 실행에는 결과가 없습니다")
    return _download(f"{run_id}.md", "text/markdown; charset=utf-8", run["answer"], download)


@router.get("/agents/{agent_id}/runs/{run_id}/readable")
def run_readable(agent_id: str, run_id: str):
    """읽기 전용 보고서(사람용 HTML) — 모델을 부르지 않고 규칙대로 바꾼다(app/runs/readable.py, 2026-09-29)."""
    from app.runs import readable
    agent = _agent(agent_id)
    run = _guard(service.get, agent_id, run_id)
    if not run.get("answer"):
        raise HTTPException(404, "이 실행에는 결과가 없습니다")
    return Response(readable.render(run, agent.get("name") or ""), media_type="text/html; charset=utf-8")


@router.get("/agents/{agent_id}/runs/{run_id}/json")
def run_json(agent_id: str, run_id: str, download: bool = False, force: bool = False):
    """결과 안의 JSON 만(다른 프로그램·에이전트가 읽는 용도). 읽을 수 없으면 404, 검사에 실패한 결과는 409(force=true 로 사람이 꺼낼 수는 있다)."""
    _agent(agent_id)
    run = _guard(service.get, agent_id, run_id)
    if run.get("status") in ("check_failed", "error", "interrupted", "running") and not force:
        raise HTTPException(409, f"이 실행은 '{run.get('status')}' 상태라 JSON 을 내보내지 않습니다(검사를 통과한 결과만 다음 단계로 넘깁니다). 사람이 확인하려면 force=true 를 붙이세요.")
    data, _, err = checks.extract_json(run.get("answer") or "")
    if err:
        raise HTTPException(404, err)
    return _download(f"{run_id}.json", "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False, indent=2), download)
