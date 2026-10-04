"""도구 API: 도구 목록(화면의 도구 탭), 그리고 상권 도구(changwon_buzz·semas_district)가 되부르는 입구."""

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.tools import registry
from app.tools.registry import ToolDef

router = APIRouter(prefix="/api", tags=["tools"])


def _public(t: ToolDef) -> dict:
    """화면에 보여 줄 도구 정보. 키 값은 절대 담지 않는다(환경변수 이름과 설정 여부만)."""
    return {
        **t.model_dump(),
        "builtin": t.id in registry.BUILTIN,
        "env": [{"name": n, "set": bool(os.getenv(n))} for n in registry.env_names(t)],
        "ready": not registry.env_missing(t),
    }


@router.get("/tools")
def list_tools():
    return {"tools": [_public(t) for t in registry.all_tools()],
            "broken": registry.broken(),   # 규칙에 어긋나 목록에서 빠진 도구와 이유(손으로 고친 tools.json)
            "limits": {"max_steps": registry.MAX_STEPS, "max_calls_per_tool": registry.MAX_CALLS_PER_TOOL, "default_daily_limit": registry.DEFAULT_DAILY_LIMIT,
                          "max_steps_task": registry.MAX_STEPS_TASK, "max_calls_per_tool_task": registry.MAX_CALLS_PER_TOOL_TASK}}


# ---------------------------------------------------------------- 지역 상권 진단(2026-09-29, 창원에서 시작)

class ChangwonBuzzIn(BaseModel):
    window_end: str
    window_start: str | None = None
    top: int = 20
    library_id: str | None = None
    region_id: str = "changwon"   # 지역 여러 개를 쓸 때 runner 가 채운다(안 주면 창원)
    area: str | None = None       # 의뢰 구역(구·동, runner 가 채운다). 비면 시 전체
    upjong: str | None = None     # 의뢰 업종(runner 가 채운다)


@router.post("/changwon/buzz")
def changwon_buzz_endpoint(body: ChangwonBuzzIn):
    """상권 화제 탐지 담당자의 changwon_buzz 도구가 부르는 곳(app/tools/changwon_buzz.py)."""
    from datetime import date, timedelta
    from app.tools import changwon_buzz
    if not body.library_id:
        raise HTTPException(422, "library_id 가 없습니다(직원의 회사에 naver_news 컴퓨터가 있어야 합니다).")
    end = body.window_end[:10]
    start = (body.window_start or "")[:10] or (date.fromisoformat(end) - timedelta(days=6)).isoformat()
    return changwon_buzz.score_library(body.library_id, start, end, max(5, min(30, body.top)), region_id=body.region_id,
                                       area=body.area, upjong=body.upjong)


class SemasIn(BaseModel):
    district: str
    region_id: str = "changwon"
    area: str | None = None   # 후보 동네 이름(runner 가 채운다). 있으면 그 동의 점포만 센다
    upjong: str | None = None   # 의뢰 업종(runner 가 채운다). 있으면 그 업종의 집중도(focus)도 돌려준다


@router.post("/semas/health")
def semas_health_endpoint(body: SemasIn):
    """상권 분석 담당자의 semas_district 도구가 부르는 곳(app/tools/semas_district.py). 구·지역은 흐름의 후보로 고정(runner 결속)."""
    from app.tools import semas_district
    return semas_district.district_health(body.district, region_id=body.region_id, area=body.area, upjong=body.upjong)
