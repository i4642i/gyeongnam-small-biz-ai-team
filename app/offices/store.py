"""사무실 저장소. 캐릭터와 같은 방식: 폴더 하나 = 사무실 하나, 목록은 폴더를 훑어서 만든다.

data/offices/o_xxxxxxxx/
    office.json   id, 과제명, 설명, 만든 날짜, 부서 목록(id·이름·책상 수), 책상 배치(assignments)

사무실 = 과제(프로젝트) 하나의 공간이다.

흐름 설정(R5): flow = {"departments": {"<부서 id>": {"mode": "parallel"|"single"}},
                      "desks": {"<부서 id>:<책상 번호>": {"task_id": "...", "inputs": {"<입력 항목>": "<앞 부서 책상 키>"}}}}
  - 실행 엔진과 검증은 app/offices/flow.py 에 있다. 여기서는 저장과 「배치가 바뀌면 설정 정리」만 한다.

책상 배치: assignments = {"<부서 id>:<책상 번호(0부터)>": "<캐릭터 id>"}
  - 한 캐릭터는 한 책상에만 앉는다(다른 사무실 포함). 다른 자리에 앉히면 원래 자리에서 옮겨진다.
  - 캐릭터가 삭제되면 unassign_agent()로 그 자리를 비운다.
"""

import json
import logging
import os
import re
import secrets
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.config import OFFICES_DIR

log = logging.getLogger("agent_town.offices")

ID_RE = re.compile(r"^o_[0-9a-f]{8}$")

# 배치는 여러 office.json 을 연달아 고치므로, 요청이 겹쳐도 서로 덮어쓰지 않게 하나씩 처리한다.
_lock = threading.RLock()


class OfficeNotFound(Exception):
    pass


class DepartmentNotFound(Exception):
    pass


class DeskOutOfRange(Exception):
    pass


class InvalidDepartment(Exception):
    """수정 요청의 부서 id가 이 사무실에 없거나 겹친다."""


class UnseatRequired(Exception):
    """이 수정으로 자리를 잃는 캐릭터가 있는데 확인(force)을 받지 못했다."""

    def __init__(self, affected: list[dict]):
        super().__init__("자리를 잃는 캐릭터가 있습니다")
        self.affected = affected   # [{"agent_id", "dept_name", "desk"(1부터)}]


def office_dir(office_id: str) -> Path:
    """id를 검증한 뒤 폴더 경로를 돌려준다. id는 URL에서 오므로 경로 조작(../)을 여기서 막는다."""
    if not ID_RE.match(office_id):
        raise OfficeNotFound(office_id)
    d = OFFICES_DIR / office_id
    if not (d / "office.json").is_file():
        raise OfficeNotFound(office_id)
    return d


def _read(d: Path) -> dict:
    office = json.loads((d / "office.json").read_text(encoding="utf-8"))
    office.setdefault("assignments", {})   # 배치 기능이 생기기 전에 만든 사무실도 그대로 읽는다
    office.setdefault("libraries", [])     # 연결한 컴퓨터 id 목록(컴퓨터 기능 전에 만든 회사는 빈 목록)
    return office


def _prune_flow(office: dict) -> None:
    """배치·부서 구성이 바뀐 뒤 흐름 설정에서 없어진 부서·책상·출처를 지운다."""
    flow = office.get("flow")
    if not flow:
        return
    dept_ids = {x["id"] for x in office["departments"]}
    seated = set(office["assignments"])
    depts_cfg = {k: v for k, v in (flow.get("departments") or {}).items() if k in dept_ids}
    flow["departments"] = depts_cfg
    repeat_dept_ids = {k for k, v in depts_cfg.items() if (v or {}).get("mode") == "repeat"}   # 3-1: 출처가 책상 키가 아니라 반복 부서 id 그 자체일 수 있다
    def _src(v):   # 연결 값은 출처 책상 키 문자열(옛 형식) 또는 {"src", "mode", "field_key"}(새 형식)
        return v.get("src") if isinstance(v, dict) else v

    desks = {}
    for key, cfg in (flow.get("desks") or {}).items():
        if key not in seated:
            continue
        cfg = dict(cfg)
        cfg["inputs"] = {n: v for n, v in (cfg.get("inputs") or {}).items() if _src(v) in seated or _src(v) in repeat_dept_ids}
        desks[key] = cfg
    flow["desks"] = desks


def set_flow(office_id: str, flow: dict) -> dict:
    """검증을 마친 흐름 설정을 저장한다(검증은 flow.validate_config)."""
    with _lock:
        d = office_dir(office_id)
        office = _read(d)
        office["flow"] = flow
        _prune_flow(office)
        _write(d, office)
        return office


def _write(d: Path, office: dict) -> None:
    # 임시 파일에 쓰고 교체한다: 쓰다가 멈춰도 office.json 이 반쯤 쓰인 채로 남지 않는다.
    tmp = d / "office.json.tmp"
    tmp.write_text(json.dumps(office, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, d / "office.json")


def create(name: str, goal: str, departments: list[dict]) -> dict:
    while True:
        office_id = "o_" + secrets.token_hex(4)
        d = OFFICES_DIR / office_id
        try:
            d.mkdir()
            break
        except FileExistsError:
            continue

    office = {
        "id": office_id,
        "name": name,
        "goal": goal,
        "created_at": datetime.now(timezone.utc).isoformat(),
        # 부서 id는 책상 배치가 가리키는 고정 식별자다. 이름은 바꿀 수 있어도 id는 유지된다.
        "departments": [
            {"id": "d_" + secrets.token_hex(3), "name": dep["name"], "desks": dep["desks"]} for dep in departments
        ],
        "assignments": {},
    }
    _write(d, office)
    return office


def get(office_id: str) -> dict:
    return _read(office_dir(office_id))


def list_all() -> list[dict]:
    offices = []
    for d in OFFICES_DIR.iterdir():
        if not (d.is_dir() and ID_RE.match(d.name) and (d / "office.json").is_file()):
            continue
        try:
            offices.append(_read(d))
        except (OSError, ValueError):
            log.warning("깨진 사무실 폴더를 건너뜁니다: %s", d, exc_info=True)
    return sorted(offices, key=lambda o: o["created_at"], reverse=True)


def delete(office_id: str) -> None:
    with _lock:
        shutil.rmtree(office_dir(office_id))


def _remove_agent(office: dict, agent_id: str, keep_key: str | None = None) -> bool:
    stale = [k for k, v in office["assignments"].items() if v == agent_id and k != keep_key]
    for k in stale:
        del office["assignments"][k]
    return bool(stale)


def assign(office_id: str, dept_id: str, desk: int, agent_id: str | None) -> dict:
    """책상 하나에 캐릭터를 앉히거나(agent_id) 비운다(None). 갱신된 사무실을 돌려준다."""
    with _lock:
        d = office_dir(office_id)
        office = _read(d)
        dept = next((x for x in office["departments"] if x["id"] == dept_id), None)
        if dept is None:
            raise DepartmentNotFound(dept_id)
        if not 0 <= desk < dept["desks"]:
            raise DeskOutOfRange(desk)

        key = f"{dept_id}:{desk}"
        if agent_id is None:
            office["assignments"].pop(key, None)
            _prune_flow(office)
        else:
            # 다른 사무실에 앉아 있었다면 그쪽에서 먼저 뺀다
            for other in list_all():
                if other["id"] != office_id and _remove_agent(other, agent_id):
                    _prune_flow(other)
                    _write(OFFICES_DIR / other["id"], other)
            _remove_agent(office, agent_id, keep_key=key)   # 같은 사무실의 다른 자리
            if office["assignments"].get(key) != agent_id and office.get("flow"):
                office["flow"]["desks"].pop(key, None)      # 다른 캐릭터가 앉으면 그 자리의 업무·입력 연결은 새로 정한다
            office["assignments"][key] = agent_id           # 이미 앉은 사람이 있으면 교체된다
            _prune_flow(office)
        _write(d, office)
        return office


def set_libraries(office_id: str, library_ids: list[str]) -> dict:
    """회사에 연결할 컴퓨터 목록을 통째로 바꾼다(존재 확인은 부르는 쪽에서). 컴퓨터는 책상이 아니라 회사 전체가 쓰는 설비다."""
    with _lock:
        d = office_dir(office_id)
        office = _read(d)
        office["libraries"] = list(dict.fromkeys(library_ids))
        _write(d, office)
        return office


def unlink_library(library_id: str) -> None:
    """컴퓨터가 삭제될 때 모든 회사에서 연결을 뗀다."""
    with _lock:
        for office in list_all():
            if library_id in (office.get("libraries") or []):
                office["libraries"] = [x for x in office["libraries"] if x != library_id]
                _write(OFFICES_DIR / office["id"], office)


def unassign_agent(agent_id: str) -> None:
    """캐릭터가 삭제될 때 모든 사무실에서 그 자리를 비운다."""
    with _lock:
        for office in list_all():
            if _remove_agent(office, agent_id):
                _prune_flow(office)
                _write(OFFICES_DIR / office["id"], office)


def update(office_id: str, name: str, goal: str, departments: list[dict], force: bool = False) -> dict:
    """사무실 정보와 부서 구성을 통째로 바꾼다.

    departments: [{"id"(기존 부서만), "name", "desks"}]
      - id가 있으면 그 부서를 고친다(이름이 바뀌어도 id와 배치는 유지).
      - id가 없으면 새 부서로 만든다.
      - 기존 부서가 목록에 없으면 삭제된 것이다.
    책상이 줄거나 부서가 사라져서 자리를 잃는 캐릭터가 있으면 force=False 일 때 UnseatRequired 를 낸다
    (아무것도 바꾸지 않는다). force=True 면 그 캐릭터들을 미배치로 만들고 저장한다.
    """
    with _lock:
        d = office_dir(office_id)
        office = _read(d)
        old = {x["id"]: x for x in office["departments"]}

        new_departments, used = [], set()
        for dep in departments:
            dep_id = dep.get("id")
            if dep_id is not None:
                if dep_id not in old or dep_id in used:
                    raise InvalidDepartment(dep_id)
            else:
                dep_id = "d_" + secrets.token_hex(3)
                while dep_id in old or dep_id in used:
                    dep_id = "d_" + secrets.token_hex(3)
            used.add(dep_id)
            new_departments.append({"id": dep_id, "name": dep["name"], "desks": dep["desks"]})

        still_valid = {f"{x['id']}:{i}" for x in new_departments for i in range(x["desks"])}
        lost = {key: agent for key, agent in office["assignments"].items() if key not in still_valid}
        if lost and not force:
            affected = []
            for key, agent_id in lost.items():
                dept_id, desk = key.split(":")
                affected.append({"agent_id": agent_id, "dept_name": old[dept_id]["name"] if dept_id in old else "", "desk": int(desk) + 1})
            affected.sort(key=lambda a: (a["dept_name"], a["desk"]))
            raise UnseatRequired(affected)

        office["name"] = name
        office["goal"] = goal
        office["departments"] = new_departments
        office["assignments"] = {k: a for k, a in office["assignments"].items() if k in still_valid}
        _prune_flow(office)
        _write(d, office)
        return office
