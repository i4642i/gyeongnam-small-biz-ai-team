"""지식 그래프 API(읽기 전용): 지도·상태·개체 상세."""

from fastapi import APIRouter, HTTPException

from app.agents import store as agents
from app.rag import graph, graph_rag, store

router = APIRouter(prefix="/api", tags=["graph"])


def _agent_or_404(agent_id: str) -> None:
    try:
        agents.agent_dir(agent_id)
    except agents.AgentNotFound:
        raise HTTPException(404, "에이전트를 찾을 수 없습니다")


def _status(agent_id: str) -> dict:
    return {**graph.coverage(agent_id), "job": graph.job_status(agent_id), "estimate": graph.estimate(agent_id),
            "settings": graph.describe_settings(agent_id), "model": graph.get_settings(agent_id)["model"]}


@router.get("/agents/{agent_id}/graph")
def get_graph(agent_id: str, limit: int = 150):
    _agent_or_404(agent_id)
    return {"status": _status(agent_id), **graph.view(agent_id, max(10, min(limit, 300)))}


@router.get("/agents/{agent_id}/graph/status")
def graph_status(agent_id: str):
    _agent_or_404(agent_id)
    return _status(agent_id)


@router.get("/agents/{agent_id}/graph/entities/{entity_id}")
def get_entity(agent_id: str, entity_id: int):
    _agent_or_404(agent_id)
    detail = graph.entity_detail(agent_id, entity_id)
    if not detail:
        raise HTTPException(404, "개체를 찾을 수 없습니다")
    return detail
