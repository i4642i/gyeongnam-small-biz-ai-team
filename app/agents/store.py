"""에이전트 저장소. 폴더 하나 = 에이전트 하나 (README '폴더 구조' 참고).

data/agents/a_xxxxxxxx/
    agent.json       id, 이름, 역할, 한 줄 소개, 만든 날짜
    persona.md       성격·말투·지시문
    appearance.json  2D 캐릭터 파츠 값
    model.json       쓸 LLM 공급사·모델
    output_format.md 기본 출력 형식
    tasks.json       정해진 업무 목록 + 자유 요청 허용 여부
    agent.db         대화 기록 (app/chat/history.py)

목록은 별도 DB 없이 폴더를 훑어서 만든다 — 진실의 원천은 폴더 하나뿐.
"""

import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from app.config import AGENTS_DIR

DEFAULT_CLAUDE_MODEL = os.getenv("CHAT_MODEL") or "claude-opus-5"

log = logging.getLogger("agent_town.store")

ID_RE = re.compile(r"^a_[0-9a-f]{8}$")


class AgentNotFound(Exception):
    pass


MAX_TOOL_HISTORY = 300   # 캐릭터당 기록 개수 상한(오래된 것부터 버림)


def _history_path(d: Path) -> Path:
    return d / "tool_history.json"


def tool_history(agent_id: str) -> list[dict]:
    """도구 변경 기록: [{at, added, removed, reason}], 최신이 앞."""
    d = agent_dir(agent_id)
    return list(reversed(_read_optional_json(_history_path(d), {}).get("entries", [])))


def _append_tool_change(d: Path, before: list[str], after: list[str], reason: str) -> None:
    added, removed = sorted(set(after) - set(before)), sorted(set(before) - set(after))
    if not added and not removed:
        return
    hp = _history_path(d)
    data = _read_optional_json(hp, {})
    entries = data.get("entries", [])
    entries.append({"at": datetime.now(timezone.utc).isoformat(), "added": added, "removed": removed, "reason": (reason or "").strip()[:200]})
    _write_json(hp, {"entries": entries[-MAX_TOOL_HISTORY:]})


def agent_dir(agent_id: str) -> Path:
    """id를 검증한 뒤 폴더 경로를 돌려준다. id는 URL에서 오므로 경로 조작(../)을 여기서 막는다."""
    if not ID_RE.match(agent_id):
        raise AgentNotFound(agent_id)
    d = AGENTS_DIR / agent_id
    if not (d / "agent.json").is_file():
        raise AgentNotFound(agent_id)
    return d


def _write_text(path: Path, text: str) -> None:
    # 임시 파일에 쓰고 교체한다: 쓰다가 멈춰도 파일이 반쯤 쓰인 채로 남지 않는다.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_json(path: Path, data: dict) -> None:
    _write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


_update_lock = threading.Lock()


def _read_optional_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def _load(d: Path) -> dict:
    info = json.loads((d / "agent.json").read_text(encoding="utf-8"))
    persona_path = d / "persona.md"
    format_path = d / "output_format.md"
    tasks = _read_optional_json(d / "tasks.json", {})
    return {
        **info,
        "persona": persona_path.read_text(encoding="utf-8") if persona_path.is_file() else "",
        "appearance": json.loads((d / "appearance.json").read_text(encoding="utf-8")),
        # 아래 세 가지는 나중에 생긴 항목이라, 이전에 만든 캐릭터에는 파일이 없다 → 기본값으로 읽는다
        "model": _read_optional_json(d / "model.json", {"provider": "anthropic", "model": DEFAULT_CLAUDE_MODEL}),
        "work": {
            "output_format": format_path.read_text(encoding="utf-8") if format_path.is_file() else "",
            "allow_free_requests": tasks.get("allow_free_requests", True),
            "strict_sources": tasks.get("strict_sources", False),   # 나중에 생긴 항목: 이전 캐릭터는 꺼짐
            "tasks": tasks.get("tasks", []),
        },
        "tools": _read_optional_json(d / "tools.json", {}).get("enabled", []),   # 나중에 생긴 항목: 이전 캐릭터는 도구 없음
    }


def create(name: str, role: str, tagline: str, persona: str, appearance: dict, model: dict, work: dict, tools: list[str] | None = None) -> dict:
    while True:
        agent_id = "a_" + secrets.token_hex(4)
        d = AGENTS_DIR / agent_id
        try:
            d.mkdir()
            break
        except FileExistsError:
            continue

    # agent.json을 마지막에 쓴다: 이 파일이 있어야 '존재하는 에이전트'로 취급되므로,
    # 중간에 실패해도 반쯤 만들어진 에이전트가 목록에 나타나지 않는다.
    (d / "persona.md").write_text(persona, encoding="utf-8")
    (d / "output_format.md").write_text(work["output_format"], encoding="utf-8")
    _write_json(d / "appearance.json", appearance)
    _write_json(d / "model.json", model)
    _write_json(d / "tasks.json", {"allow_free_requests": work["allow_free_requests"], "strict_sources": work.get("strict_sources", False), "tasks": work["tasks"]})
    _write_json(d / "tools.json", {"enabled": list(tools or [])})
    _append_tool_change(d, [], list(tools or []), "캐릭터 생성")
    _write_json(d / "agent.json", {
        "id": agent_id, "name": name, "role": role, "tagline": tagline,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return _load(d)


def get(agent_id: str) -> dict:
    return _load(agent_dir(agent_id))


def list_all() -> list[dict]:
    agents = []
    for d in AGENTS_DIR.iterdir():
        if not (d.is_dir() and ID_RE.match(d.name) and (d / "agent.json").is_file()):
            continue
        try:
            info = _load(d)
        except (OSError, ValueError, KeyError):
            log.warning("깨진 에이전트 폴더를 건너뜁니다: %s", d, exc_info=True)
            continue
        if info.get("internal_run"):   # 예전 검증 실행용 임시 캐릭터 — 화면 목록에는 안 보인다
            continue
        agents.append(info)
    return sorted(agents, key=lambda a: a["created_at"], reverse=True)


# ---------------------------------------------------------------- 예전 실행별 임시(숨김) 캐릭터 — 뒷정리 전용
# 2026-09-23~27 검증 실행마다 만들던 숨김 캐릭터(agent.json 의 internal_run 표시). 회사 컴퓨터로 바꾸면서 더 만들지
# 않는다. 예전에 남은 것이 있으면 서버를 켤 때 flow._sweep_orphan_ephemeral_agents 가 아래 두 함수로 지운다.

def list_ephemeral(flow_id: str | None = None) -> list[dict]:
    """internal_run 이 있는 임시 캐릭터 전부(또는 flow_id 로 좁혀서). 화면 목록에는 안 뜨므로 list_all() 과
    별도로 훑는다 — 정리(cleanup)·고아 정리 전용."""
    out = []
    for d in AGENTS_DIR.iterdir():
        if not (d.is_dir() and ID_RE.match(d.name) and (d / "agent.json").is_file()):
            continue
        try:
            info = _load(d)
        except (OSError, ValueError, KeyError):
            continue
        run = info.get("internal_run")
        if not run or (flow_id is not None and run.get("flow_id") != flow_id):
            continue
        out.append(info)
    return out


def delete_ephemeral(agent_id: str) -> None:
    """예전 임시 캐릭터(internal_run 표시)만 지운다 — internal_run 표시가 없으면(진짜 캐릭터면) 거부한다,
    실수로 진짜 캐릭터를 지우는 사고를 코드 수준에서 막기 위해(2026-09-23)."""
    info = get(agent_id)
    if not info.get("internal_run"):
        raise ValueError(f"{agent_id} 는 임시 캐릭터가 아닙니다(internal_run 없음) — delete_ephemeral 로 지울 수 없습니다.")
    delete(agent_id)


def delete(agent_id: str) -> None:
    # 윈도우에서는 방금까지 쓰던 파일(벡터 색인 등)이 잠시 잠겨 있을 수 있어서 몇 번 다시 시도한다
    d = agent_dir(agent_id)
    for attempt in range(8):
        try:
            shutil.rmtree(d)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.5)


def update(agent_id: str, name: str, role: str, tagline: str, persona: str, appearance: dict, model: dict, work: dict, tools: list[str] | None = None,
           change_reason: str = "") -> dict:
    """캐릭터 정보를 통째로 바꾼다. id·만든 날짜·대화 기록(agent.db)·문서는 그대로 두고,
    사무실 배치는 캐릭터 id 로 이어지므로 이름을 바꿔도 유지된다."""
    with _update_lock:
        d = agent_dir(agent_id)
        info = json.loads((d / "agent.json").read_text(encoding="utf-8"))
        before_tools = _read_optional_json(d / "tools.json", {}).get("enabled", [])
        _write_text(d / "persona.md", persona)
        _write_text(d / "output_format.md", work["output_format"])
        _write_json(d / "appearance.json", appearance)
        _write_json(d / "model.json", model)
        _write_json(d / "tasks.json", {"allow_free_requests": work["allow_free_requests"], "strict_sources": work.get("strict_sources", False), "tasks": work["tasks"]})
        if tools is not None:   # 보내지 않으면 지금 설정을 그대로 둔다
            _write_json(d / "tools.json", {"enabled": list(tools)})
            _append_tool_change(d, before_tools, list(tools), change_reason)
        # agent.json 을 마지막에 쓴다 (이름·역할은 목록에 바로 보이는 값이라, 나머지가 다 저장된 뒤에 바꾼다)
        _write_json(d / "agent.json", {**info, "name": name, "role": role, "tagline": tagline})
        return _load(d)
