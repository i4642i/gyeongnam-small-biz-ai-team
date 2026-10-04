"""사무실 흐름(R5 1단계): 사무실을 「실행 가능한 흐름」으로 만든다.

  사무실 = 파이프라인 하나, 부서 = 단계(위에서 아래 순서), 책상 = 캐릭터 + 그 캐릭터의 정해진 업무 하나.
  부서 실행 방식: parallel(동시) — 부서의 모든 책상을 함께 실행하고 전부 끝날 때까지 기다린 뒤 다음 부서로 /
                  single(단일)   — 책상 하나.
  데이터 전달: 앞 부서 책상의 결과 JSON 이 다음 부서 업무의 입력 항목으로 들어간다(책상마다 「입력 항목 ← 출처 책상」).
  사무실 공통 입력: 사무실 실행 때 한 번 넣은 값(관측 기간 등)은 출처가 정해지지 않은 입력 항목에 이름이 같으면 전달된다.
  검증 관문: 업무 실행의 상태가 「성공(ok)」이고 결과에서 JSON 을 읽을 수 있어야만 다음 부서로 넘어간다. 아니면 흐름이 멈춘다.

저장: data/offices/<id>/office.json 의 flow(설정) · data/offices/<id>/flows/f_xxxxxxxx.json(실행 기록).
업무 실행 자체는 기존 app/runs/service.py 를 그대로 쓴다(재시도·검증·훅·토큰 기록이 그대로 적용된다).
"""

import copy
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from app.agents import store as agent_store
from app.offices import store as office_store
from app.runs import cancel
from app.runs import checks
from app.runs import service as runs
from app.tools import registry as tools_registry

log = logging.getLogger("agent_town.flow")

MODES = ("parallel", "single", "repeat")
MODE_LABEL = {"parallel": "동시", "single": "단일", "repeat": "반복"}
REPEAT_DEFAULT_CONCURRENT = 3
REPEAT_MAX_CONCURRENT = 10
CONTENT_MODES = ("full", "field", "fields", "summary", "full_report")
# fields(2026-09-28, 숙제 E3): 최상위 키 여러 개만 남긴 JSON — 반복 부서의 모은 결과면 항목마다 적용한다.
# 운용 매니저가 검증·동종 보고서의 인용문·지표 표까지 받아 입력이 15만 자가 넘던 문제를 줄인다.
CONTENT_MODE_LABEL = {"full": "JSON 전체", "field": "특정 항목만(최상위 키)", "fields": "지정한 키만(항목마다)",
                      "summary": "A절 요약", "full_report": "보고서 전체"}
FLOW_ID = re.compile(r"^f_[0-9a-f]{8}$")
MAX_FLOWS_KEPT = 100
POLL_SECONDS = float(os.getenv("FLOW_POLL_SECONDS") or 1.5)

_live: dict[str, dict] = {}       # flow_id → 지금 도는 흐름의 최신 상태(파일을 바꿔 쓰는 순간의 조회 충돌을 피한다)
_active: dict[str, str] = {}      # office_id → 지금 도는 flow_id
_lock = threading.RLock()

# 프롬프트 결재 관문(2026-09-25): 각 책상은 LLM 에 보내기 전에 프롬프트를 만들어 두고 "결재 대기"로 멈춘다.
# 사람이 승인(approve)한 책상만 실제로 모델을 부르고, 거부(reject)한 책상은 건너뛴다. 서버가 꺼지면 이 표시는
# 사라지고(흐름은 recover 가 '중단됨' 처리), 사람이 다시 실행한다.
_approved: dict[str, set] = {}    # flow_id → 승인된 desk_key 집합
_rejected: dict[str, set] = {}    # flow_id → 거부된 desk_key 집합
_stopping: set[str] = set()       # '작업 정지'가 요청된 flow_id. 실행 루프가 확인해 멈추고, 흐름을 'stopped' 로 마감한다.
APPROVAL_POLL = 0.4               # 승인 대기 중 확인 간격(초)


def request_stop(office_id: str, flow_id: str) -> dict:
    """'작업 정지': 지금 도는 흐름을 멈춘다. 아직 안 보낸(결재 대기·대기) 책상은 보내지 않고,
    이미 도는 책상은 다음 모델 왕복을 취소한다(진행 중이던 한 번은 끝난다). 흐름은 'stopped' 로 마감된다."""
    office_store.get(office_id)   # 없으면 OfficeNotFound
    with _lock:
        if _active.get(office_id) != flow_id:
            raise FlowError("지금 실행 중인 흐름이 아닙니다(이미 끝났거나 정지되었습니다).", 409)
        flow = _live.get(flow_id) or get(office_id, flow_id)
        _stopping.add(flow_id)
        run_ids = [x.get("run_id") for dep in flow["departments"] for x in dep["desks"]
                   if x.get("status") in ("running", "paused") and x.get("run_id")]
        _active.pop(office_id, None)   # 결재 대기·부서 대기 루프가 '멈춤'으로 빠져나오게 한다
    for rid in run_ids:
        cancel.request(rid)            # 이미 도는 책상: 다음 모델 왕복을 하지 않는다
    log.info("작업 정지 요청 (%s/%s), 취소한 실행 %d개", office_id, flow_id, len(run_ids))
    return summary(flow)


def approve_desk(flow_id: str, desk_key: str) -> None:
    with _lock:
        _approved.setdefault(flow_id, set()).add(desk_key)


def reject_desk(flow_id: str, desk_key: str) -> None:
    with _lock:
        _rejected.setdefault(flow_id, set()).add(desk_key)


def _approval_state(flow_id: str, desk_key: str) -> str:
    """'approved' | 'rejected' | 'waiting'."""
    with _lock:
        if desk_key in _rejected.get(flow_id, set()):
            return "rejected"
        if desk_key in _approved.get(flow_id, set()):
            return "approved"
    return "waiting"


def _clear_approvals(flow_id: str) -> None:
    with _lock:
        _approved.pop(flow_id, None)
        _rejected.pop(flow_id, None)


class FlowError(Exception):
    def __init__(self, message: str, code: int = 422, problems: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.problems = problems or []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- 계획(설정 + 배치 → 실행할 책상 목록)

def get_config(office: dict) -> dict:
    cfg = office.get("flow") or {}
    out = {"departments": dict(cfg.get("departments") or {}), "desks": dict(cfg.get("desks") or {})}
    if cfg.get("prepare"):
        out["prepare"] = dict(cfg["prepare"])   # 흐름 전체의 시작 전 준비(2026-09-29)
    return out


def dept_mode(cfg: dict, dept_id: str) -> str:
    m = (cfg["departments"].get(dept_id) or {}).get("mode")
    return m if m in MODES else "parallel"


def dept_repeat(cfg: dict, dept_id: str) -> dict | None:
    return (cfg["departments"].get(dept_id) or {}).get("repeat")


def _refers(task: dict, name: str) -> bool:
    """수행 지시가 이 입력 항목을 {이름} 또는 {{이름}} 으로 쓰는가(안 쓰면 앞 단계 결과가 모델에게 전달되지 않는다)."""
    return re.search(r"\{\{?\s*" + re.escape(name) + r"\s*\}\}?", task.get("instruction") or "") is not None


def _norm_conn(v) -> dict:
    """저장된 연결 값(옛 형식은 출처 책상 키 문자열, 새 형식은 {src, mode, field_key})을 한 모양으로 맞춘다."""
    if isinstance(v, str):
        return {"src": v, "mode": "full", "field_key": ""}
    if isinstance(v, dict):
        mode = v.get("mode") if v.get("mode") in CONTENT_MODES else "full"
        return {"src": v.get("src") or "", "mode": mode, "field_key": (v.get("field_key") or "").strip()}
    return {"src": "", "mode": "full", "field_key": ""}


def build_plan(office: dict) -> dict:
    """화면과 실행이 함께 쓰는 계획. problems 가 있으면 실행할 수 없고, warnings 는 알림이다."""
    cfg = get_config(office)
    problems, warnings = [], []
    depts, order, desk_info = [], {}, {}
    for di, dept in enumerate(office["departments"]):
        mode = dept_mode(cfg, dept["id"])
        repeat = dept_repeat(cfg, dept["id"]) if mode == "repeat" else None
        desks = []
        for i in range(dept["desks"]):
            key = f"{dept['id']}:{i}"
            agent_id = office["assignments"].get(key)
            if not agent_id:
                continue
            try:
                agent = agent_store.get(agent_id)
            except agent_store.AgentNotFound:
                continue       # 삭제된 캐릭터의 흔적은 빈 자리로 본다
            tasks = agent["work"]["tasks"]
            dcfg = cfg["desks"].get(key) or {}
            task = next((t for t in tasks if t.get("id") == dcfg.get("task_id")), tasks[0] if tasks else None)
            label = f"{dept['name']} {i + 1}번 책상({agent['name']})"
            item = {"key": key, "index": i, "agent": {"id": agent["id"], "name": agent["name"], "role": agent["role"]},
                    "tasks": [{"id": t["id"], "name": t["name"]} for t in tasks], "task_id": task["id"] if task else None,
                    "task_name": task["name"] if task else None, "inputs": [], "label": label}
            if task is None:
                problems.append(f"{label}: 이 캐릭터에게 정해진 업무가 없습니다(캐릭터 수정 → 업무·출력 탭).")
            else:
                missing_tools = [t for t in (task.get("required_tools") or []) if t not in (agent.get("tools") or [])]
                if missing_tools:
                    problems.append(f"{label}: 이 업무에 필요한 도구가 꺼져 있습니다: {missing_tools} (캐릭터 수정 → 도구 탭).")
                if repeat and repeat["input_name"] not in {i["name"] for i in task["inputs"]}:
                    problems.append(f"{label}: 반복 입력 이름 '{repeat['input_name']}'이 이 업무의 입력 항목에 없습니다.")
                defaults = runs.defaults(agent, task)
                for inp in task["inputs"]:
                    is_repeat_item = bool(repeat and inp["name"] == repeat["input_name"])
                    conn = _norm_conn((dcfg.get("inputs") or {}).get(inp["name"]))
                    item["inputs"].append({"name": inp["name"], "type": inp["type"], "required": inp["required"], "options": inp["options"],
                                           "default": inp.get("default") or "", "default_value": defaults.get(inp["name"], ""), "source": conn["src"],
                                           "mode": conn["mode"], "mode_label": CONTENT_MODE_LABEL[conn["mode"]], "field_key": conn["field_key"],
                                           "referenced": _refers(task, inp["name"]), "is_repeat_item": is_repeat_item})
            order[key] = di
            desk_info[key] = (agent, task, item)
            desks.append(item)
        if mode == "repeat":
            order[dept["id"]] = di   # 반복 부서는 책상 키(템플릿 하나) 말고 부서 id 자체도 출처로 쓸 수 있다 — "모은 결과"를 가리킨다(3-1)
        if mode == "single" and len(desks) > 1:
            problems.append(f"{dept['name']}: 실행 방식이 「단일」인데 앉은 캐릭터가 {len(desks)}명입니다(한 명만 앉히거나 「동시」로 바꾸세요).")
        if mode == "repeat":
            if len(desks) != 1:
                problems.append(f"{dept['name']}: 실행 방식이 「반복」이면 반복할 캐릭터 한 명만 앉혀야 합니다(지금 {len(desks)}명).")
            if not repeat:
                problems.append(f"{dept['name']}: 실행 방식이 「반복」인데 반복 설정(출처·배열 이름)이 없습니다.")
        if not desks:
            warnings.append(f"{dept['name']}: 앉은 캐릭터가 없어 건너뜁니다.")
        depts.append({"id": dept["id"], "name": dept["name"], "index": di, "mode": mode, "mode_label": MODE_LABEL[mode],
                      "repeat": repeat, "desks": desks})

    # 출처 검사: 앞 부서의 앉은 책상(또는 반복 부서 자신의 모은 결과)이어야 하고, 받는 업무의 수행 지시가 그 입력 항목을 써야 한다
    repeat_dept_ids = {dep["id"] for dep in depts if dep["mode"] == "repeat"}
    for dep in depts:
        if dep["mode"] == "repeat" and dep["repeat"]:
            src = dep["repeat"]["source"]
            if src not in order or order[src] >= dep["index"]:
                problems.append(f"{dep['name']}: 반복 출처 책상 {src} 가 앞 부서의 앉은 책상이 아닙니다.")
        for item in dep["desks"]:
            for inp in item["inputs"]:
                if inp.get("is_repeat_item"):
                    continue   # 반복 항목 입력은 실행할 때마다 목록에서 하나씩 채워 넣는다(출처 연결이 아니다)
                src = inp["source"]
                if not src:
                    continue
                if src not in order or order[src] >= dep["index"]:
                    problems.append(f"{item['label']}: 입력 '{inp['name']}'의 출처가 앞 부서의 앉은 책상이 아닙니다.")
                elif not inp["referenced"]:
                    problems.append(f"{item['label']}: 입력 '{inp['name']}'에 앞 단계 결과를 넣지만 이 업무의 수행 지시에 {{{{{inp['name']}}}}} 가 없어 모델에게 전달되지 않습니다.")
                elif src in repeat_dept_ids and inp["mode"] not in ("full", "fields"):
                    problems.append(f"{item['label']}: 입력 '{inp['name']}'의 출처가 반복 부서의 모은 결과라 「넘길 내용」은 'JSON 전체' 또는 '지정한 키만(항목마다)'만 됩니다(성공한 항목만 배열로 모아 넘깁니다).")
                elif inp["mode"] in ("field", "fields") and not inp["field_key"]:
                    problems.append(f"{item['label']}: 입력 '{inp['name']}'의 「넘길 내용」이 '특정 항목만'인데 최상위 키 이름이 비어 있습니다.")
    if not any(dep["desks"] for dep in depts):
        problems.append("실행할 책상이 없습니다. 캐릭터를 책상에 앉히세요.")

    # 사무실 공통 입력: 출처가 정해지지 않은 입력 항목(이름 기준으로 합친다) — 반복 항목 입력은 제외(실행 중에 채워짐)
    common: dict[str, dict] = {}
    for dep in depts:
        for item in dep["desks"]:
            for inp in item["inputs"]:
                if inp["source"] or inp.get("is_repeat_item"):
                    continue
                c = common.get(inp["name"])
                if c is None:
                    common[inp["name"]] = {"name": inp["name"], "type": inp["type"], "required": inp["required"], "options": list(inp["options"]),
                                           "default": inp["default"], "default_value": inp["default_value"]}
                    continue
                c["required"] = c["required"] or inp["required"]
                if c["type"] == "choice" and inp["type"] == "choice":     # 같은 이름의 고르기 항목은 모든 책상이 받아들이는 값만 남긴다
                    c["options"] = [o for o in c["options"] if o in inp["options"]]
                    if c["default_value"] and c["default_value"] not in c["options"]:
                        c["default_value"] = ""
                    if not c["options"]:
                        problems.append(f"입력 '{inp['name']}': 책상마다 고를 수 있는 값이 겹치지 않습니다({item['label']}).")
                elif c["type"] != inp["type"]:
                    warnings.append(f"입력 '{inp['name']}': 책상마다 종류가 다릅니다({c['type']} / {inp['type']}). 첫 책상 기준으로 검사합니다.")
    return {"departments": depts, "common_inputs": list(common.values()), "problems": problems, "warnings": warnings}


def _validate_prepare(office: dict, did: str, p) -> dict:
    """반복 부서의 '시작 전 준비': 회사 컴퓨터 하나에 설치된 도구 하나를 항목마다 실행하고 결과가 준비될 때까지 기다린다.
    args 값: "@item.필드"(반복 항목의 값), "@공통입력이름"(예: @window_end), 그 밖은 그대로 쓴다."""
    from app.libraries import store as lib_store
    from app.tools import registry
    if not isinstance(p, dict):
        raise FlowError(f"{did}: 시작 전 준비 설정 형식이 올바르지 않습니다.")
    lid, tid = str(p.get("computer") or ""), str(p.get("tool") or "")
    if lid not in (office.get("libraries") or []):
        raise FlowError(f"{did}: 시작 전 준비의 컴퓨터가 이 회사에 놓여 있지 않습니다.")
    try:
        lib = lib_store.get(lid)
    except lib_store.LibraryNotFound:
        raise FlowError(f"{did}: 시작 전 준비의 컴퓨터를 찾을 수 없습니다.")
    tool = registry.get(tid)
    if tool is None or tid not in lib.get("tools", []):
        raise FlowError(f"{did}: '{lib['name']}'에 설치되지 않은 도구입니다: {tid}")
    args = {}
    params = {x.name: x for x in tool.params}
    for k, v in (p.get("args") or {}).items():
        if k not in params:
            raise FlowError(f"{did}: '{tool.label or tool.id}' 도구에 없는 인자입니다: {k}")
        v = str(v).strip()
        if v:
            args[k] = v[:200]
    out = {"computer": lid, "tool": tid, "args": args}
    wait = str(p.get("wait") or "index")
    if wait not in ("index", "download"):
        raise FlowError(f"{did}: 기다릴 단계는 index(색인까지) 또는 download(받기까지) 중 하나입니다.")
    if wait == "download":
        out["wait"] = "download"
    ex = p.get("expand")
    if ex:
        # 목록으로 펼치기(2026-09-28, 동종 비교): 항목마다 목록 도구를 먼저 불러 받은 목록(예: 후보+비교군 티커)의
        # 원소마다 컴퓨터 도구를 실행한다. 원소는 into 인자 자리에 들어간다.
        if not isinstance(ex, dict):
            raise FlowError(f"{did}: 목록으로 펼치기 설정 형식이 올바르지 않습니다.")
        etid, field, into = str(ex.get("tool") or ""), str(ex.get("field") or "").strip(), str(ex.get("into") or "").strip()
        etool = registry.get(etid)
        if etool is None or etool.format != "json" or not etool.http:
            raise FlowError(f"{did}: 목록 도구는 JSON 을 돌려주는 HTTP 도구여야 합니다: {etid or '(비어 있음)'}")
        if not field or into not in params:
            raise FlowError(f"{did}: 목록 이름(field)과, 원소를 넣을 '{tool.label or tool.id}' 인자(into)를 정해야 합니다.")
        eparams = {x.name for x in etool.params}
        eargs = {}
        for k, v in (ex.get("args") or {}).items():
            if k not in eparams:
                raise FlowError(f"{did}: '{etool.label or etool.id}' 도구에 없는 인자입니다: {k}")
            if str(v).strip():
                eargs[k] = str(v).strip()[:200]
        args.setdefault(into, "@list")   # 필수 인자 검사를 통과시키는 자리표시(실행 때 원소로 바뀐다)
        out["expand"] = {"tool": etid, "args": eargs, "field": field, "into": into}
    missing = [x.name for x in tool.params if x.required and x.name not in args]
    if missing:
        raise FlowError(f"{did}: 시작 전 준비에 필수 인자가 비었습니다: {', '.join(missing)}")
    return out


def validate_config(office: dict, raw: dict) -> dict:
    """화면이 보낸 설정을 엄격하게 검사해 저장할 모양으로 정리한다(잘못되면 FlowError)."""
    if not isinstance(raw, dict):
        raise FlowError("흐름 설정 형식이 올바르지 않습니다.")
    dept_ids = [d["id"] for d in office["departments"]]
    pos = {d: i for i, d in enumerate(dept_ids)}
    out = {"departments": {}, "desks": {}}
    for did, v in (raw.get("departments") or {}).items():
        if did not in pos:
            raise FlowError(f"이 사무실에 없는 부서입니다: {did}")
        v = v or {}
        mode = v.get("mode")
        if mode not in MODES:
            raise FlowError(f"실행 방식은 {', '.join(MODES)} 중 하나여야 합니다.")
        entry = {"mode": mode}
        if mode == "repeat":
            r = v.get("repeat") or {}
            src = (r.get("source") or "").strip()
            field = (r.get("field") or "").strip()
            input_name = (r.get("input_name") or "candidate").strip()
            if not src or ":" not in src:
                raise FlowError(f"{did}: 반복할 목록의 출처 책상을 지정해야 합니다.")
            sdid = src.split(":")[0]
            if src not in office["assignments"] or sdid not in pos:
                raise FlowError(f"{did}: 반복 출처 책상 {src} 에 앉은 캐릭터가 없습니다.")
            if pos[sdid] >= pos[did]:
                raise FlowError(f"{did}: 반복 출처는 앞 부서의 책상이어야 합니다.")
            if not field:
                raise FlowError(f"{did}: 반복할 배열의 이름(최상위 키)을 지정해야 합니다.")
            if not input_name:
                raise FlowError(f"{did}: 항목 하나를 받을 입력 이름을 지정해야 합니다.")
            try:
                max_conc = int(r.get("max_concurrent") or REPEAT_DEFAULT_CONCURRENT)
            except (TypeError, ValueError):
                raise FlowError(f"{did}: 동시 실행 수는 숫자여야 합니다.")
            if not (1 <= max_conc <= REPEAT_MAX_CONCURRENT):
                raise FlowError(f"{did}: 동시 실행 수는 1~{REPEAT_MAX_CONCURRENT} 사이여야 합니다.")
            entry["repeat"] = {"source": src, "field": field, "input_name": input_name, "max_concurrent": max_conc}
            merge = r.get("merge_from_root") or []   # 항목마다 같이 넘길 출처 결과의 최상위 키(예: 팀장 favored_traits → 동종 비교)
            if isinstance(merge, str):
                merge = [x.strip() for x in merge.split(",")]
            if not isinstance(merge, list) or not all(isinstance(x, str) for x in merge):
                raise FlowError(f"{did}: 같이 넘길 최상위 키는 이름 목록이어야 합니다.")
            merge = [x.strip() for x in merge if x.strip()]
            if any(x == field for x in merge):
                raise FlowError(f"{did}: 같이 넘길 키에 반복할 목록 이름({field})을 넣을 수 없습니다.")
            if merge:
                entry["repeat"]["merge_from_root"] = merge[:10]
            if r.get("prepare"):
                entry["repeat"]["prepare"] = _validate_prepare(office, did, r["prepare"])
        out["departments"][did] = entry
    for key, v in (raw.get("desks") or {}).items():
        did = key.split(":")[0]
        agent_id = office["assignments"].get(key)
        if did not in pos or not agent_id:
            raise FlowError(f"앉은 캐릭터가 없는 책상입니다: {key}")
        try:
            agent = agent_store.get(agent_id)
        except agent_store.AgentNotFound:
            raise FlowError(f"캐릭터를 찾을 수 없습니다: {key}")
        tasks = agent["work"]["tasks"]
        v = v or {}
        task = next((t for t in tasks if t["id"] == v.get("task_id")), None)
        if v.get("task_id") and task is None:
            raise FlowError(f"{key}: 그 캐릭터에게 없는 업무입니다.")
        task = task or (tasks[0] if tasks else None)
        inputs = {}
        for name, raw_conn in (v.get("inputs") or {}).items():
            conn = _norm_conn(raw_conn)
            src = conn["src"]
            if not src:
                continue
            if task is None or name not in {i["name"] for i in task["inputs"]}:
                raise FlowError(f"{key}: 업무에 없는 입력 항목입니다: {name}")
            if ":" in src:   # 앞 부서의 책상 하나
                sdid = src.split(":")[0]
                if src not in office["assignments"] or sdid not in pos:
                    raise FlowError(f"{key}: 출처 책상 {src} 에 앉은 캐릭터가 없습니다.")
                src_pos = pos[sdid]
            else:            # 반복 부서 자신(모은 결과 전체) — 3-1
                if src not in pos:
                    raise FlowError(f"{key}: 출처 부서 {src} 가 이 사무실에 없습니다.")
                if (out["departments"].get(src) or {}).get("mode") != "repeat":
                    raise FlowError(f"{key}: '{src}'는 반복 부서가 아니라 부서 전체를 출처로 쓸 수 없습니다(책상을 지정하세요).")
                src_pos = pos[src]
                if conn["mode"] not in ("full", "fields"):
                    raise FlowError(f"{key}: 반복 부서 전체가 출처면 「넘길 내용」은 'full' 또는 'fields'만 됩니다.")
            if conn["mode"] == "fields" and not conn["field_key"]:
                raise FlowError(f"{key}: '{name}'의 「넘길 내용」에 남길 키 이름(콤마로 구분)이 비어 있습니다.")
            if src_pos >= pos[did]:
                raise FlowError(f"{key}: 출처는 앞 부서여야 합니다(같은 부서나 뒤 부서는 안 됩니다).")
            if len(conn["field_key"]) > 200:
                raise FlowError(f"{key}: '{name}'의 최상위 키 이름이 너무 깁니다.")
            inputs[name] = {"src": src, "mode": conn["mode"], "field_key": conn["field_key"]}
        out["desks"][key] = {"task_id": task["id"] if task else None, "inputs": inputs}
    if raw.get("prepare"):
        # 흐름 전체의 시작 전 준비(2026-09-29, 뉴스 회사): 첫 부서 전에 회사 컴퓨터 도구를 한 번 실행하고 색인까지 기다린다.
        # 인자 값은 "@공통입력이름"(예: @window_end) 또는 그대로. 반복 항목(@item.)은 없다.
        p = _validate_prepare(office, "시작 전 준비", raw["prepare"])
        if p.get("expand") or any(v.startswith("@item.") for v in p["args"].values()):
            raise FlowError("흐름 전체의 시작 전 준비에는 반복 항목(@item.)·목록 펼치기를 쓸 수 없습니다.")
        out["prepare"] = p
    return out


# ---------------------------------------------------------------- 저장

def _flows_dir(office_id: str):
    d = office_store.office_dir(office_id) / "flows"
    d.mkdir(exist_ok=True)
    return d


def _path(office_id: str, flow_id: str):
    if not FLOW_ID.match(flow_id):
        raise FlowError("실행 기록을 찾을 수 없습니다", 404)
    return _flows_dir(office_id) / f"{flow_id}.json"


_save_lock = threading.Lock()   # 반복 부서는 여러 스레드가 같은 흐름을 동시에 저장한다 — 직렬화·쓰기를 한 줄로 세운다


def _save(flow: dict) -> None:
    """흐름 기록 저장. 예전에는 스레드들이 같은 임시 파일(.json.tmp)을 함께 써서, 한 스레드가 옮긴 뒤 다른 스레드의 os.replace 가
    FileNotFoundError 로 죽었다(2026-09-27 ASB 책상: 실행은 끝났는데 결과 기록을 놓침). 이제 스냅샷과 쓰기를 한 잠금 안에서 하고,
    다른 스레드가 흐름을 고치는 도중이라 직렬화가 실패하면 잠깐 뒤 다시 한다."""
    p = _path(flow["office_id"], flow["id"])
    tmp = p.with_suffix(f".json.{threading.get_ident()}.tmp")
    with _save_lock:
        for attempt in range(6):
            try:
                with _lock:
                    snap = copy.deepcopy(flow)
                    _live[flow["id"]] = snap
                tmp.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, p)
                return
            except (PermissionError, RuntimeError):   # 윈도우: 누가 읽는 중인 파일은 못 바꿈 / 다른 스레드가 흐름을 고치는 중(dict 크기 바뀜)
                if attempt == 5:
                    raise
                time.sleep(0.2)


def get(office_id: str, flow_id: str) -> dict:
    p = _path(office_id, flow_id)
    with _lock:
        live = _live.get(flow_id)
    if live is not None:
        return copy.deepcopy(live)
    if not p.is_file():
        raise FlowError("실행 기록을 찾을 수 없습니다", 404)
    return json.loads(p.read_text(encoding="utf-8"))


def summary(flow: dict) -> dict:
    """목록용: 결과 JSON(outputs)은 빼고 부서별 상태만."""
    out = {k: flow.get(k) for k in ("id", "office_id", "office_name", "status", "stage", "inputs", "started_at", "finished_at", "duration_s", "error", "stopped_at", "totals")}
    out["resumes"] = flow.get("resumes") or []
    out["departments"] = [{"id": d["id"], "name": d["name"], "mode": d["mode"], "status": d["status"],
                           "desks": [{k: x.get(k) for k in ("key", "agent_id", "agent_name", "task_name", "status", "stage", "run_id", "error", "warnings")} for x in d["desks"]]} for d in flow["departments"]]
    return out


def detail(flow: dict) -> dict:
    out = summary(flow)
    out["departments"] = copy.deepcopy(flow["departments"])
    out["output_chars"] = {k: len(json.dumps(v, ensure_ascii=False)) for k, v in (flow.get("outputs") or {}).items()}
    out["has_final_report"] = bool(flow.get("final_report_md"))   # 4부: 최종 보고서는 따로 /report 로 받는다(화면 목록에선 있는지만)
    return out


def list_flows(office_id: str, limit: int = 30) -> list[dict]:
    out = []
    for p in _flows_dir(office_id).glob("f_*.json"):
        with _lock:
            f = copy.deepcopy(_live.get(p.stem))
        try:
            f = f or json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append(f)
    out.sort(key=lambda f: f.get("started_at", ""), reverse=True)
    return [summary(f) for f in out[:limit]]


def running(office_id: str) -> str | None:
    with _lock:
        return _active.get(office_id)


def delete(office_id: str, flow_id: str) -> None:
    with _lock:
        if _active.get(office_id) == flow_id:
            raise FlowError("실행 중인 기록은 지울 수 없습니다", 409)
    p = _path(office_id, flow_id)
    if not p.is_file():
        raise FlowError("실행 기록을 찾을 수 없습니다", 404)
    p.unlink()
    with _lock:
        _live.pop(flow_id, None)


def recover() -> None:
    """서버가 꺼지면서 끝나지 못한 흐름을 '중단됨'으로 표시한다(그 흐름이 시작한 업무 실행은 runs.recover 가 따로 정리한다)."""
    for o in office_store.list_all():
        d = office_store.OFFICES_DIR / o["id"] / "flows"
        if not d.is_dir():
            continue
        for p in d.glob("f_*.json"):
            try:
                f = json.loads(p.read_text(encoding="utf-8"))
                if f.get("status") == "running":
                    f.update(status="interrupted", stage="서버가 꺼져 중단됨", finished_at=_now())
                    for dep in f["departments"]:
                        for x in dep["desks"]:
                            if x["status"] in ("pending", "running"):
                                x["status"] = "interrupted"
                        if dep["status"] in ("pending", "running"):
                            dep["status"] = "interrupted"
                    p.write_text(json.dumps(f, ensure_ascii=False), encoding="utf-8")
            except (OSError, ValueError):
                log.warning("흐름 기록을 읽지 못했습니다: %s", p)
    _sweep_orphan_ephemeral_agents()   # 위에서 흐름들을 먼저 '중단됨'으로 표시한 뒤, 이미 끝난 흐름의 고아 임시 캐릭터만 정리


# ---------------------------------------------------------------- 실행

def _clean_common(plan: dict, raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise FlowError("입력값 형식이 올바르지 않습니다.")
    known = {c["name"]: c for c in plan["common_inputs"]}
    extra = [k for k in raw if k not in known]
    if extra:
        raise FlowError(f"이 사무실 흐름에 없는 입력 항목입니다: {extra[0]} (있는 항목: {', '.join(known) or '없음'})")
    out = {}
    for name, v in raw.items():
        v = "" if v is None else str(v).strip()
        if not v:
            continue
        c = known[name]
        if len(v) > 2000:
            raise FlowError(f"'{name}'이(가) 너무 깁니다(2000자 이내).")
        if c["type"] == "choice" and v not in c["options"]:
            raise FlowError(f"'{name}'은(는) {', '.join(c['options'])} 중 하나여야 합니다.")
        if c["type"] == "number":
            try:
                float(v)
            except ValueError:
                raise FlowError(f"'{name}'은(는) 숫자여야 합니다.")
        out[name] = v
    return out


def _flow_dept(dep: dict) -> dict:
    """계획(build_plan)의 부서 하나를 실행 기록에 남길 모양으로 바꾼다. 반복 부서는 책상을 실행할 때 채운다
    (그 전엔 몇 개일지 모른다 — 출처 책상의 배열 길이에 달려 있다)."""
    if dep["mode"] == "repeat":
        x = dep["desks"][0]   # 반복할 캐릭터(템플릿) — 이 자체는 실행되지 않는다
        return {"id": dep["id"], "name": dep["name"], "mode": "repeat", "status": "pending", "desks": [],
                "repeat": {**dep["repeat"], "template_agent_id": x["agent"]["id"], "template_agent_name": x["agent"]["name"],
                          "template_agent_role": x["agent"]["role"], "template_task_id": x["task_id"], "template_task_name": x["task_name"]}}
    return {"id": dep["id"], "name": dep["name"], "mode": dep["mode"], "status": "pending",
            "desks": [{"key": x["key"], "agent_id": x["agent"]["id"], "agent_name": x["agent"]["name"], "agent_role": x["agent"]["role"],
                       "task_id": x["task_id"], "task_name": x["task_name"], "status": "pending", "stage": "", "run_id": None, "error": None,
                       "sources": {i["name"]: i["source"] for i in x["inputs"] if i["source"]},
                       "input_conn": {i["name"]: {"mode": i["mode"], "mode_label": i["mode_label"], "field_key": i["field_key"]}
                                      for i in x["inputs"] if i["source"]},
                       "input_chars": {}, "duration_s": None, "usage": None, "tool_calls": None}
                      for x in dep["desks"]]}


def start(office_id: str, raw_inputs: dict, auto_approve: bool = False) -> dict:
    office = office_store.get(office_id)
    plan = build_plan(office)
    if plan["problems"]:
        raise FlowError("이 사무실 흐름을 실행할 수 없습니다: " + " / ".join(plan["problems"]), 422, plan["problems"])
    values = _clean_common(plan, raw_inputs or {})
    desks = [x for dep in plan["departments"] for x in dep["desks"]]

    # 실행 전에 미리 확인: 필수 입력이 채워지는가, 앉은 캐릭터가 다른 업무를 실행 중이지 않은가
    missing, busy = [], []
    for dep in plan["departments"]:
        for x in dep["desks"]:
            for inp in x["inputs"]:
                if inp["source"] or not inp["required"] or inp.get("is_repeat_item"):
                    continue
                if not (values.get(inp["name"]) or inp["default_value"]):
                    missing.append(f"{x['label']}의 '{inp['name']}'")
            if runs.is_running(x["agent"]["id"]):
                busy.append(x["agent"]["name"])
    if missing:
        raise FlowError("필수 입력이 비어 있습니다: " + ", ".join(missing) + ". 사무실 공통 입력에 값을 넣어 주세요.")
    if busy:
        raise FlowError("다른 업무를 실행 중인 캐릭터가 있습니다: " + ", ".join(busy) + ". 끝난 뒤에 다시 실행하세요.", 409)

    with _lock:
        if office_id in _active:
            raise FlowError("이 사무실은 이미 흐름을 실행 중입니다.", 409)
        if len(list(_flows_dir(office_id).glob("f_*.json"))) >= MAX_FLOWS_KEPT:
            raise FlowError(f"실행 기록이 {MAX_FLOWS_KEPT}개입니다. 오래된 기록을 지운 뒤 실행하세요.", 409)
        flow_id = "f_" + secrets.token_hex(4)
        _active[office_id] = flow_id
    try:
        flow = {"id": flow_id, "office_id": office_id, "office_name": office["name"], "status": "running", "stage": "대기", "inputs": values,
                "started_at": _now(), "finished_at": None, "duration_s": None, "error": None, "stopped_at": None, "totals": None, "outputs": {},
                "auto_approve": bool(auto_approve),
                "departments": [_flow_dept(dep) for dep in plan["departments"] if dep["desks"]]}
        _save(flow)
    except Exception:
        with _lock:
            _active.pop(office_id, None)
        raise
    threading.Thread(target=_run_from, args=(flow, values, 0), name=f"flow-{flow_id}", daemon=True).start()
    return summary(flow)


def start_seeded(office_id: str, raw_inputs: dict, seed_desk_key: str, seed_run_id: str, seed_data: dict) -> dict:
    """새 흐름을 시작하되, seed_desk_key 책상은 실행하지 않고 이미 다른 실행(seed_run_id)에서 나온 결과를
    그 자리에 채워 넣은 채로 시작한다(예: 이번 주 팀장 결과를 재사용해 기자·팀장을 다시 부르지 않기 위해).
    이 부서는 즉시 "ok"로 표시되고, 그 뒤 부서부터 정상적으로 실행된다. seed_data 에는 "_seeded_from_run"
    표시를 붙여 새로 실행된 결과와 구분한다(화면·최종 보고서 모두에서 보인다)."""
    office = office_store.get(office_id)
    plan = build_plan(office)
    if plan["problems"]:
        raise FlowError("이 사무실 흐름을 실행할 수 없습니다: " + " / ".join(plan["problems"]), 422, plan["problems"])
    seed_dep = next((dep for dep in plan["departments"] if any(x["key"] == seed_desk_key for x in dep["desks"])), None)
    if seed_dep is None:
        raise FlowError(f"심을 책상을 찾을 수 없습니다: {seed_desk_key}")
    if seed_dep["mode"] == "repeat":
        raise FlowError("반복 부서의 책상에는 심을 수 없습니다(반복 부서는 앞 부서 결과에서 매번 새로 만들어집니다).")
    values = _clean_common(plan, raw_inputs or {})

    missing, busy = [], []
    for dep in plan["departments"]:
        if dep["index"] <= seed_dep["index"]:
            continue   # 심은 부서와 그 앞은 실행하지 않으니 입력·바쁨 확인이 필요 없다
        for x in dep["desks"]:
            for inp in x["inputs"]:
                if inp["source"] or not inp["required"] or inp.get("is_repeat_item"):
                    continue
                if not (values.get(inp["name"]) or inp["default_value"]):
                    missing.append(f"{x['label']}의 '{inp['name']}'")
            if runs.is_running(x["agent"]["id"]):
                busy.append(x["agent"]["name"])
    if missing:
        raise FlowError("필수 입력이 비어 있습니다: " + ", ".join(missing) + ". 사무실 공통 입력에 값을 넣어 주세요.")
    if busy:
        raise FlowError("다른 업무를 실행 중인 캐릭터가 있습니다: " + ", ".join(busy) + ". 끝난 뒤에 다시 실행하세요.", 409)

    with _lock:
        if office_id in _active:
            raise FlowError("이 사무실은 이미 흐름을 실행 중입니다.", 409)
        if len(list(_flows_dir(office_id).glob("f_*.json"))) >= MAX_FLOWS_KEPT:
            raise FlowError(f"실행 기록이 {MAX_FLOWS_KEPT}개입니다. 오래된 기록을 지운 뒤 실행하세요.", 409)
        flow_id = "f_" + secrets.token_hex(4)
        _active[office_id] = flow_id
    try:
        flow = {"id": flow_id, "office_id": office_id, "office_name": office["name"], "status": "running", "stage": "대기", "inputs": values,
                "started_at": _now(), "finished_at": None, "duration_s": None, "error": None, "stopped_at": None, "totals": None, "outputs": {},
                "departments": [_flow_dept(dep) for dep in plan["departments"] if dep["desks"]]}
        seeded = dict(seed_data)
        seeded["_seeded_from_run"] = seed_run_id   # 새로 만든 것과 구분하는 표시 — 최종 보고서·화면·다운로드 JSON 모두에 남는다
        dep_rec = next(d for d in flow["departments"] if d["id"] == seed_dep["id"])
        desk_rec = next(x for x in dep_rec["desks"] if x["key"] == seed_desk_key)
        desk_rec.update(status="ok", stage="이전 실행 결과를 재사용", run_id=seed_run_id, error=None, duration_s=0, usage=None, tool_calls=0)
        desk_rec["seeded_from_run"] = seed_run_id
        dep_rec["status"] = "ok"
        flow["outputs"][seed_desk_key] = seeded
        _save(flow)
    except Exception:
        with _lock:
            _active.pop(office_id, None)
        raise
    threading.Thread(target=_run_from, args=(flow, values, seed_dep["index"] + 1), name=f"flow-{flow_id}-seeded", daemon=True).start()
    return summary(flow)


def resume(office_id: str, flow_id: str, department_id: str, reuse_done: bool = False) -> dict:
    """"여기서부터 다시": 이 부서와 그 뒤 부서만 다시 실행한다. 앞 부서의 결과(outputs)는 그대로 쓴다.

    다시 실행할 부서 목록은 이번 실행이 시작될 때 저장해 둔 부서·책상 목록("배치 버전", flow["departments"])을 그대로 따른다
    — 사무실 설정이 그 뒤 바뀌었어도 이 실행 기록 안에서는 처음 돌 때의 구성으로 재현한다. 캐릭터의 지시문·도구 내용 자체는
    (배치가 아니라 캐릭터 쪽 설정이므로) 다시 실행하는 시점의 최신 내용을 쓴다."""
    office_store.get(office_id)   # 사무실이 있는지만 확인(존재하지 않으면 OfficeNotFound)
    with _lock:
        if office_id in _active:
            raise FlowError("이 사무실은 이미 흐름을 실행 중입니다.", 409)
        flow = get(office_id, flow_id)
        idx = next((i for i, dep in enumerate(flow["departments"]) if dep["id"] == department_id), None)
        if idx is None:
            raise FlowError("이 실행 기록에 없는 부서입니다(그때 이 부서에 앉은 캐릭터가 없었을 수 있습니다).", 404)
        _active[office_id] = flow_id
    first_keys = None
    first = flow["departments"][idx]
    if reuse_done and first["mode"] == "repeat" and first["desks"]:
        # "끝난 것은 두고 이어서": 이 부서에서 이미 성공한 책상(과 실행은 끝났는데 기록을 놓친 책상)은 그대로 쓰고 나머지만 돈다
        _adopt_finished(flow, first["desks"])
        first_keys = {x["key"] for x in first["desks"] if x["status"] != "ok"}
    for dep in flow["departments"][idx:]:
        dep["status"] = "pending"
        for x in dep["desks"]:
            if first_keys is not None and dep is first and x["key"] not in first_keys:
                continue   # 끝난 책상은 결과 유지
            x.update(status="pending", stage="", run_id=None, error=None, duration_s=None, usage=None, tool_calls=None, input_chars={}, warnings=None)
            flow["outputs"].pop(x["key"], None)   # 지난 시도의 결과를 지운다(새로 쓰기 전까지 묵은 값이 남지 않게)
    if first_keys is not None:
        flow["outputs"].pop(first["id"], None)   # 부서 모음 결과는 새로 만든다
    scope = " (끝난 책상은 그대로 두고 나머지만)" if first_keys is not None else ""
    flow["status"], flow["stage"], flow["error"], flow["stopped_at"], flow["finished_at"] = "running", f"{first['name']}부터 다시 실행{scope}", None, None, None
    flow.setdefault("resumes", []).append({"department_id": department_id, "department": first["name"], "at": _now(),
                                           **({"scope": "reuse_done", "kept": sum(1 for x in first["desks"] if x["key"] not in first_keys)} if first_keys is not None else {})})
    try:
        _save(flow)
    except Exception:
        with _lock:
            _active.pop(office_id, None)
        raise
    threading.Thread(target=_run_from, args=(flow, flow["inputs"], idx), kwargs={"first_desk_keys": first_keys},
                     name=f"flow-{flow_id}-resume", daemon=True).start()
    return summary(flow)


def resume_desk(office_id: str, flow_id: str, desk_key: str) -> dict:
    """"이 책상만 다시": 같은 부서의 나머지 책상은 이전 실행 결과를 그대로 쓰고, 이 책상 하나만 다시 실행한다.
    부서 전체가 아니라 책상 하나가 실패했을 때(이번처럼 도구 하나가 빠져 있던 경우 등) 나머지 책상까지 다시 돌리지 않아
    비용이 부서 인원수분의 1로 준다. 이 부서가 다시 성공하면 그 뒤 부서는 resume() 과 똑같이 이어서 돈다."""
    office_store.get(office_id)
    with _lock:
        if office_id in _active:
            raise FlowError("이 사무실은 이미 흐름을 실행 중입니다.", 409)
        flow = get(office_id, flow_id)
        dep_idx, dep, desk = None, None, None
        for i, d in enumerate(flow["departments"]):
            hit = next((x for x in d["desks"] if x["key"] == desk_key), None)
            if hit:
                dep_idx, dep, desk = i, d, hit
                break
        if dep_idx is None:
            raise FlowError("이 실행 기록에 없는 책상입니다(그때 이 책상에 캐릭터가 없었을 수 있습니다).", 404)
        _active[office_id] = flow_id
    desk.update(status="pending", stage="", run_id=None, error=None, duration_s=None, usage=None, tool_calls=None, input_chars={})
    flow["outputs"].pop(desk_key, None)
    for later in flow["departments"][dep_idx + 1:]:   # 뒤 부서는 이 책상의 새 결과를 받을 수 있으니 전부 다시 돈다(부서 단위 재개와 동일)
        later["status"] = "pending"
        for x in later["desks"]:
            x.update(status="pending", stage="", run_id=None, error=None, duration_s=None, usage=None, tool_calls=None, input_chars={}, warnings=None)
            flow["outputs"].pop(x["key"], None)
    flow["status"], flow["stage"], flow["error"], flow["stopped_at"], flow["finished_at"] = "running", f"{dep['name']}의 {desk['agent_name']} 책상만 다시 실행", None, None, None
    flow.setdefault("resumes", []).append({"department_id": dep["id"], "department": dep["name"], "desk_key": desk_key, "desk_agent": desk["agent_name"], "scope": "desk", "at": _now()})
    try:
        _save(flow)
    except Exception:
        with _lock:
            _active.pop(office_id, None)
        raise
    threading.Thread(target=_run_from, args=(flow, flow["inputs"], dep_idx), kwargs={"first_desk_keys": {desk_key}},
                      name=f"flow-{flow_id}-resume-desk", daemon=True).start()
    return summary(flow)


def _fail_text(run: dict) -> str:
    st = run.get("status")
    if st == "error":
        return run.get("error") or "오류로 끝났습니다."
    if st in ("check_failed", "warning"):
        probs = (run.get("check") or {}).get("problems") or []
        head = "검사에 실패했습니다" if st == "check_failed" else "검사에 실패했지만 설정(실패 시 경고)에 따라 저장된 결과라 다음 부서로 넘기지 않습니다"
        return head + (": " + " / ".join(p[:120] for p in probs[:3]) if probs else "")
    if st == "interrupted":
        return "서버가 꺼져 중단되었습니다."
    return f"상태가 '{st}'입니다."


def _run_from(flow: dict, common: dict, start_index: int, first_desk_keys: set[str] | None = None) -> None:
    """flow["departments"][start_index] 부터 끝까지 순서대로 실행한다. start_index=0 이면 처음 실행, 그 밖이면 resume().
    first_desk_keys 가 있으면 start_index 부서는 그 책상들만 다시 실행한다(나머지 책상은 이전 결과 유지) — resume_desk() 전용."""
    t0 = time.time()
    office_id = flow["office_id"]
    try:
        if start_index == 0 and not first_desk_keys and not _run_flow_prepare(flow, common):
            with _lock:
                stopped = flow["id"] in _stopping or _active.get(office_id) != flow["id"]
            flow["status"] = "stopped" if stopped else "failed"
            flow["stage"] = "시작 전 준비에서 " + ("정지됨" if stopped else "멈춤")
            for dep in flow["departments"]:
                dep["status"] = "skipped"
                for x in dep["desks"]:
                    x["status"] = "skipped"
            return
        for di, dep in enumerate(flow["departments"]):
            if di < start_index:
                continue
            desk_keys = first_desk_keys if di == start_index else None
            if not _run_department(flow, dep, common, desk_keys=desk_keys):
                with _lock:
                    stopped = flow["id"] in _stopping
                if stopped:                       # '작업 정지'로 멈춘 것 — 실패가 아니라 정지로 마감한다
                    flow["status"], flow["stage"] = "stopped", f"{dep['name']}에서 정지됨"
                    flow["stopped_at"] = {"department_id": dep["id"], "department": dep["name"],
                                          "desks": [x["key"] for x in dep["desks"] if x["status"] == "stopped"]}
                else:
                    flow["status"] = "failed"
                    flow["stopped_at"] = {"department_id": dep["id"], "department": dep["name"], "desks": [x["key"] for x in dep["desks"] if x["status"] == "failed"]}
                    flow["stage"] = f"{dep['name']}에서 멈춤"
                for later in flow["departments"][di + 1:]:
                    later["status"] = "skipped"
                    for x in later["desks"]:
                        x["status"] = "skipped"
                break
        else:
            flow["status"], flow["stage"] = "ok", "완료"
            try:
                from app.offices import final_report
                md = final_report.build(flow)
                if md:
                    flow["final_report_md"] = md   # 4부: 운용팀까지 끝났으면 최종 보고서를 조립해 실행 기록에 붙인다(모델 호출 없음)
            except Exception:
                log.exception("최종 보고서 조립 실패(무시 — 흐름 결과 자체는 그대로 둔다)")
    except Exception as e:
        log.exception("사무실 흐름 실행 오류 (%s)", office_id)
        flow["status"], flow["stage"], flow["error"] = "error", "오류", f"실행하지 못했습니다: {type(e).__name__}"
    finally:
        flow["finished_at"] = _now()
        flow["duration_s"] = round(time.time() - t0, 1)
        flow["totals"] = _totals(flow)
        try:
            _save(flow)
        finally:
            with _lock:
                _active.pop(office_id, None)
                _live.pop(flow["id"], None)
                _stopping.discard(flow["id"])   # 정지 표시 정리
            _clear_approvals(flow["id"])   # 이 흐름의 승인·거부 표시 정리(메모리)


def _totals(flow: dict) -> dict:
    t = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "tool_calls": 0, "runs": 0, "warnings": 0}
    for dep in flow["departments"]:
        for x in dep["desks"]:
            u = x.get("usage") or {}
            if x.get("run_id"):
                t["runs"] += 1
            t["model_calls"] += u.get("calls", 0) or 0
            t["input_tokens"] += u.get("input_tokens", 0) or 0
            t["output_tokens"] += u.get("output_tokens", 0) or 0
            t["cache_read_tokens"] += u.get("cache_read_tokens", 0) or 0
            t["cache_write_tokens"] += u.get("cache_write_tokens", 0) or 0
            t["tool_calls"] += x.get("tool_calls") or 0
            t["warnings"] += len(x.get("warnings") or [])   # 경고 통과 건수(형식 허용 목록) — 흐름 합계 로그
    return t


def _find_desk(flow: dict, key: str) -> dict | None:
    for dep in flow["departments"]:
        for x in dep["desks"]:
            if x["key"] == key:
                return x
    return None


def _stringify(v) -> str:
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _repeat_output_list(agg: dict) -> list:
    """반복 부서의 모은 결과({covered, not_covered, items:[{index,status,result}]})에서
    성공(ok)한 항목의 result 만 뽑아 순서대로 배열로 만든다(3-1: not_covered 는 빠진다)."""
    if not isinstance(agg, dict):
        return []
    return [it.get("result") for it in (agg.get("items") or [])
            if it.get("status") == "ok" and it.get("result") is not None]


def _keep_keys(obj, keys: list[str]):
    """dict 면 지정한 최상위 키만(있는 것만), 그 밖은 그대로."""
    return {k: obj[k] for k in keys if k in obj} if isinstance(obj, dict) else obj


def _content_for(flow: dict, src: str, data, mode: str, field_key: str) -> str:
    """연결마다 고른 「넘길 내용」에 맞춰 실제로 모델에게 넘길 글을 만든다."""
    keys = [k.strip() for k in (field_key or "").split(",") if k.strip()]
    if any(dep["id"] == src and dep.get("mode") == "repeat" for dep in flow["departments"]):
        items = _repeat_output_list(data)   # 반복 부서 전체가 출처면 항상 "성공한 항목만 배열로"
        if mode == "fields" and keys:
            items = [_keep_keys(it, keys) for it in items]
        return _stringify(items)
    if mode == "fields" and keys:
        return _stringify(_keep_keys(data, keys))
    if mode == "field":
        if isinstance(data, dict) and field_key in data:
            return _stringify(data[field_key])
        keys = ", ".join(data.keys()) if isinstance(data, dict) else "없음"
        return f"(이 보고서에 최상위 키 '{field_key}' 가 없습니다. 있는 키: {keys})"
    if mode == "summary":
        if isinstance(data, dict):
            for k in ("summary_ko", "summary"):
                if k in data:
                    return _stringify(data[k])
        return "(이 보고서에 summary_ko/summary 항목이 없어 요약을 넘기지 못했습니다.)"
    if mode == "full_report":
        sx = _find_desk(flow, src)
        if sx and sx.get("run_id"):
            try:
                r = runs.get(sx["agent_id"], sx["run_id"])
                return r.get("answer") or _stringify(data)
            except runs.RunError:
                pass
        return _stringify(data)
    return _stringify(data)   # full: JSON 전체


def _desk_inputs(flow: dict, agent: dict, task: dict, x: dict, common: dict) -> tuple[dict, set, dict]:
    """책상 하나의 입력값: 앞 부서 결과(고른 「넘길 내용」대로) → 사무실 공통 입력 → 업무 기본값 순."""
    defaults = runs.defaults(agent, task)
    conn = x.get("input_conn") or {}
    values, long_names, input_chars = {}, set(), {}
    for inp in task["inputs"]:
        name = inp["name"]
        src = x["sources"].get(name)
        if src:
            c = conn.get(name) or {"mode": "full", "field_key": ""}
            text = _content_for(flow, src, flow["outputs"][src], c.get("mode") or "full", c.get("field_key") or "")
            values[name] = text
            long_names.add(name)
            input_chars[name] = len(text)
        else:
            values[name] = common.get(name) or defaults.get(name, "")
    return values, long_names, input_chars


def _attach_evidence(flow: dict, x: dict, r: dict, data) -> None:
    """검증 보고서 JSON에 이 실행이 회사 컴퓨터에서 실제로 꺼내 본 문서의 정보(파일·서식·게재일·근거)를 덧붙여
    flow.json 에 함께 남긴다 — 나중에 컴퓨터 문서가 바뀌어도 이 스냅샷으로 무엇을 근거로 했는지 알 수 있다.
    지식그래프 근거(graph_relations_used·graph_build)는 아직 빈 채로 둔다. data 를 그 자리에서 바꾼다."""
    from app.rag import store as rag_store
    seen, documents = set(), []
    for src in r.get("sources") or []:
        for it in src.get("items") or []:
            lib, doc_id = it.get("library_id"), it.get("doc_id")
            if not lib or doc_id is None or (lib, doc_id) in seen:
                continue
            seen.add((lib, doc_id))
            try:
                doc = rag_store.get_document(lib, doc_id)
            except agent_store.AgentNotFound:
                doc = None
            if doc:
                documents.append({"computer_id": lib, "filename": doc["filename"], "doc_type": doc.get("doc_type"),
                                  "published_at": doc.get("published_at"), "date_basis": doc.get("date_basis"),
                                  "date_evidence": doc.get("date_evidence")})
    if isinstance(data, dict):
        data["_evidence"] = {
            "collected_documents": documents,
            "graph_relations_used": [],   # 지식그래프 근거 — 아직 안 만든다
            "graph_build": None,
        }


def _sweep_orphan_ephemeral_agents() -> None:
    """(예전 방식 뒷정리) 검증 실행마다 만들던 임시 캐릭터는 2026-09-27 회사 컴퓨터로 바꾸면서 더 만들지 않는다.
    예전에 서버가 비정상 종료돼 안 지워진 임시 캐릭터가 남아 있으면 정리한다 — 그 flow_id 의 흐름 기록이 이미 끝난 상태일
    때만 지운다(아직 running 으로 보이면, 그 흐름 자체의 복구가 끝난 뒤 처리되게 건드리지 않는다). recover()
    끝에서 부른다(흐름 자체를 '중단됨'으로 먼저 표시한 뒤)."""
    for info in agent_store.list_ephemeral():
        run = info.get("internal_run") or {}
        flow_id = run.get("flow_id")
        if not flow_id:
            continue
        found = None
        for o in office_store.list_all():
            p = _flows_dir(o["id"]) / f"{flow_id}.json"
            if p.is_file():
                found = p
                break
        if found is None:
            continue   # 흐름 기록을 못 찾음(지워졌을 수 있음) — 안전하게 그대로 둔다, 사람이 보고 지울 수 있다
        try:
            f = json.loads(found.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if f.get("status") == "running":
            continue
        try:
            agent_store.delete_ephemeral(info["id"])
        except Exception:
            log.exception("고아 임시 캐릭터 정리 실패 (%s)", info["id"])


def _prompt_path(flow: dict, desk_key: str):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", desk_key)   # desk_key 에 콜론(d_xxx:0) 등이 있어 파일명으로 정리
    return _flows_dir(flow["office_id"]) / f"{flow['id']}__{safe}.prompt.txt"


def read_desk_prompt(office_id: str, flow_id: str, desk_key: str) -> str | None:
    """결재용으로 저장해 둔 이 책상의 프롬프트 미리보기(파일). 없으면 None."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", desk_key)
    p = _flows_dir(office_id) / f"{flow_id}__{safe}.prompt.txt"
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def _build_prompt_preview(agent: dict, task: dict, values: dict) -> str:
    """실제로 모델에 보낼 프롬프트를 코드로 조립한다(모델 호출 없음). 시스템 프롬프트 + 업무 입력(user).
    실행 시 여기에 RAG 조각·도구 결과가 더 붙을 수 있으나, 애널리스트는 그 부분이 대체로 비어 있어 이 미리보기가
    실제 전송 내용의 대부분이다."""
    from app.chat import llm
    from app.tools import registry
    tools = [t for tid in agent.get("tools", []) if (t := registry.get(tid)) is not None]
    # 실제 실행과 같은 출력 한도·검색 예산을 미리보기에도 반영한다(결재 화면이 실제 전송 내용과 일치하도록).
    search_budget = task.get("search_budget") or registry.DEFAULT_SEARCH_BUDGET
    system = llm.build_system_prompt(agent, hits=None, graph_info=None, tools=tools, mode="task",
                                     max_output_tokens=task.get("max_output_tokens"), search_budget=search_budget)
    user = runs.render(task, values)
    return (f"=== 결재용 프롬프트 미리보기 ===\n캐릭터: {agent.get('name','')} ({agent['id']})\n"
            f"업무: {task.get('name','')}\n모델: {(agent.get('model') or {}).get('provider','')} / {(agent.get('model') or {}).get('model','')}\n\n"
            f"──────── 시스템 프롬프트 ({len(system):,}자) ────────\n{system}\n\n"
            f"──────── 업무 입력 (user, {len(user):,}자) ────────\n{user}\n")


def _stage_for_approval(flow: dict, x: dict, agent: dict, task: dict, values: dict) -> None:
    """책상을 '결재 대기'로 만든다: 프롬프트를 조립해 파일로 저장하고 상태를 바꾼다(아직 모델 안 부름)."""
    try:
        preview = _build_prompt_preview(agent, task, values)
        _prompt_path(flow, x["key"]).write_text(preview, encoding="utf-8")
        x["prompt_ready"] = True
    except Exception as e:
        log.exception("프롬프트 미리보기 생성 실패 (%s)", x["key"])
        x["prompt_ready"] = False
        x["prompt_error"] = f"{type(e).__name__}: {e}"
    x["status"], x["stage"], x["error"] = "awaiting_approval", "결재 대기(승인해야 전송)", None
    x["upstream_warnings"] = _upstream_warnings(flow, x)
    if flow.get("auto_approve"):   # 상담소에서 의뢰한 흐름(2026-09-30): 프롬프트는 결재 자료로 남기고 승인은 자동으로 한다
        approve_desk(flow["id"], x["key"])


def _desk_label(x: dict) -> str:
    item = x.get("repeat_item")
    tag = (item.get("ticker") or item.get("name")) if isinstance(item, dict) else (item if isinstance(item, str) else None)
    return f"{x.get('agent_name') or x['key']}" + (f" · {tag}" if tag else "")


def _upstream_warnings(flow: dict, x: dict) -> list[dict]:
    """이 책상보다 앞 부서들에서 '경고 통과'로 넘어온 결과 — 결재 카드에 보여 준다(경고는 실행 뒤에 생기므로 받는 쪽 카드에 표시)."""
    out = []
    for dep in flow.get("departments") or []:
        if any(d["key"] == x["key"] for d in dep["desks"]):
            break
        for d in dep["desks"]:
            if d.get("warnings") and d.get("status") == "ok":
                out.append({"desk": _desk_label(d), "items": d["warnings"]})
    return out


def _wait_for_approval(flow: dict, x: dict) -> bool:
    """이 책상이 승인될 때까지 기다린다. 승인되면 True, 거부되면 False. 흐름이 멈추면(_active 에서 빠지면) False.
    모델은 승인된 뒤에야 호출된다 — 승인 전에는 아무것도 전송하지 않는다."""
    office_id, flow_id = flow["office_id"], flow["id"]
    while True:
        state = _approval_state(flow_id, x["key"])
        if state == "approved":
            return True
        if state == "rejected":
            x["status"], x["stage"], x["error"] = "rejected", "결재 거부됨(전송 안 함)", "사람이 이 책상을 거부했습니다."
            return False
        with _lock:
            if _active.get(office_id) != flow_id:   # 흐름이 멈췄다(서버 재시작·중단 등)
                return False
        time.sleep(APPROVAL_POLL)


COMPUTER_POLL = 5.0                # 회사 컴퓨터 준비 확인 간격(초)


def _current_repeat(flow: dict, dep: dict) -> dict:
    """지금 회사의 흐름 설정에 있는 이 부서의 반복 설정(없으면 빈 dict)."""
    try:
        office = office_store.get(flow["office_id"])
    except office_store.OfficeNotFound:
        return {}
    return (((office.get("flow") or {}).get("departments") or {}).get(dep["id"]) or {}).get("repeat") or {}


def _current_prepare(flow: dict, dep: dict) -> dict | None:
    """실행 기록에 준비 설정이 없으면(설정 전에 시작한 흐름을 다시 돌릴 때) 지금 회사의 흐름 설정에서 가져온다."""
    return _current_repeat(flow, dep).get("prepare")


def _prepare_args(prepare: dict, item, common: dict) -> dict:
    out = {}
    for k, v in (prepare.get("args") or {}).items():
        if v.startswith("@item."):
            val = (item or {}).get(v[len("@item."):]) if isinstance(item, dict) else None
        elif v.startswith("@"):
            val = common.get(v[1:])
        else:
            val = v
        if val not in (None, ""):
            out[k] = str(val)
    return out


def _expand_list(expand: dict, item, common: dict) -> list[str]:
    """목록으로 펼치기: 목록 도구(JSON HTTP 도구)를 항목의 값으로 불러 field 목록을 돌려준다. 실패하면 빈 목록 대신
    항목 자체의 into 값(예: 후보 티커 하나)만 쓴다 — 적어도 원래 하던 일은 한다."""
    from app.tools import registry, runner
    tool = registry.get(expand["tool"])
    args = _prepare_args({"args": expand.get("args") or {}}, item, common)
    fallback = [args[k] for k in args][:1]
    if tool is None:
        return fallback
    res = runner.execute(tool, args, runner.Context(agent_id="flow", mode="task"), track=False)
    lines = res.text.splitlines()
    body = "\n".join(lines[2:-1]) if len(lines) >= 3 and lines[0].startswith("<도구결과") else res.text
    try:
        vals = json.loads(body).get(expand["field"])
    except (ValueError, AttributeError):
        vals = None
    if not res.ok or not isinstance(vals, list) or not vals:
        return fallback
    return list(dict.fromkeys(str(v) for v in vals if str(v).strip()))[:30]


def _wait_for_computer(flow: dict, dep: dict, targets: list[dict], common: dict, prepare: dict) -> list[dict] | None:
    """흐름 설정의 '시작 전 준비'대로, 회사 컴퓨터에 설치된 도구를 항목마다 실행하게 하고 결과(문서 색인)가 준비되거나
    실패할 때까지 기다린다. 시간 제한은 없다(진행 상황을 부서·책상 단계에 보여 주고, 사람이 '작업 정지'로 멈출 수 있다).
    준비 못 한 항목은 결재를 올리지 않고 not_covered 로 남긴다. 실행할 책상 목록을 돌려준다(멈췄으면 None)."""
    from app.libraries import collect, store as lib_store

    try:
        pc = lib_store.get(prepare["computer"])
    except lib_store.LibraryNotFound:
        flow.setdefault("notes", []).append(f"{dep['name']}: 시작 전 준비의 컴퓨터를 찾을 수 없어 건너뜀")
        return targets
    # 항목마다 실행할 인자 목록. 목록으로 펼치기(expand)가 있으면 항목 하나가 여러 작업(예: 후보 + 비교군 공시)이 된다.
    per_target: list[list[dict]] = []
    for x in targets:
        base = _prepare_args(prepare, x.get("repeat_item"), common)
        if prepare.get("expand"):
            base.pop(prepare["expand"]["into"], None)
            per_target.append([{**base, prepare["expand"]["into"]: v} for v in _expand_list(prepare["expand"], x.get("repeat_item"), common)])
        else:
            per_target.append([base])
    flat = [a for lst in per_target for a in lst]
    try:
        jobs = collect.request(pc["id"], prepare["tool"], flat,
                               requested_by=f"{flow.get('office_name', '')} 흐름 {flow['id']} · {dep['name']}",
                               reason="부서 시작 전 준비")
    except collect.CollectError as e:
        flow.setdefault("notes", []).append(f"{dep['name']}: 시작 전 준비를 못 함 — {e}")
        return targets
    it = iter(jobs)
    jobs_of = {x["key"]: [next(it)["id"] for _ in lst] for x, lst in zip(targets, per_target)}
    for x in targets:
        x["prepare_job"] = {"computer": pc["id"], "job": jobs_of[x["key"]][0] if jobs_of[x["key"]] else None, "jobs": jobs_of[x["key"]]}

    def combined(state: dict, key: str) -> dict:
        """항목 하나의 작업들을 합친 상태. 펼친 목록은 일부 실패(예: SEC 공시가 없는 비교군)해도 하나라도 준비되면 준비됨."""
        sts = [state[j] for j in jobs_of[key]]
        if not sts:
            return {"state": "failed", "note": "목록이 비어 실행할 작업이 없습니다"}
        if any(s["state"] == "waiting" for s in sts):
            done = sum(1 for s in sts if s["state"] != "waiting")
            return {"state": "waiting", "note": (f"{done}/{len(sts)} · " if len(sts) > 1 else "") + next(s["note"] for s in sts if s["state"] == "waiting")}
        if not prepare.get("expand"):
            return sts[0]
        ok = sum(1 for s in sts if s["state"] == "ready")
        return {"state": "ready", "note": f"{ok}/{len(sts)} 준비"} if ok else {"state": "failed", "note": "; ".join(s["note"] for s in sts)[:300]}

    while True:
        raw_state = collect.readiness(pc["id"], list(dict.fromkeys(j for v in jobs_of.values() for j in v)),
                                      require_index=prepare.get("wait") != "download")
        state = {x["key"]: combined(raw_state, x["key"]) for x in targets}
        ready = sum(1 for x in targets if state[x["key"]]["state"] == "ready")
        waiting = any(v["state"] == "waiting" for v in state.values())
        for x in targets:
            st = state[x["key"]]
            x["stage"] = {"ready": f"{pc['name']} 준비됨" + (f"({st['note']})" if st["note"] else ""),
                          "waiting": f"{pc['name']} 준비 중 — {st['note']}"}.get(st["state"], f"{pc['name']}: 준비 실패")
        flow["stage"] = f"{dep['name']}: {pc['name']} 준비 중 {ready}/{len(targets)}" if waiting else f"{dep['name']} 실행 중"
        _save(flow)
        if not waiting:
            break
        with _lock:
            if _active.get(flow["office_id"]) != flow["id"]:
                return None
        time.sleep(COMPUTER_POLL)
    runnable = []
    for x in targets:
        st = state[x["key"]]
        if st["state"] == "ready":
            runnable.append(x)
        else:
            x["status"], x["stage"] = "not_covered", ""
            x["error"] = f"{pc['name']}가 이 항목의 자료를 준비하지 못해 실행하지 않았습니다: {st['note']}"
    _save(flow)
    return runnable


def _run_flow_prepare(flow: dict, common: dict) -> bool:
    """흐름 전체의 시작 전 준비(설정 prepare, 2026-09-29): 회사 컴퓨터 도구를 한 번 실행하고, 작업이 끝나고
    받은 문서의 색인이 끝날 때까지 기다린다. 새로 받은 문서가 없어도(이미 다 가진 기사) 작업이 성공이면 준비된 것으로 본다.
    설정이 없으면 바로 True. 실패·정지면 False(첫 부서를 시작하지 않는다)."""
    from app.libraries import collect, store as lib_store
    from app.rag import store as rag_store
    try:
        prepare = get_config(office_store.get(flow["office_id"])).get("prepare")
    except office_store.OfficeNotFound:
        prepare = None
    if not prepare:
        return True
    if (flow.get("prepare") or {}).get("state") == "ready":
        return True   # 다시 실행(resume)할 때 이미 끝낸 준비는 되풀이하지 않는다
    # 의뢰 지역(2026-09-30): 뉴스는 그 지역 전용 컴퓨터에 그 지역 검색어로 모은다(다른 지역 기사가 섞이지 않게)
    from app import regions
    _rid, rcfg = regions.of_inputs(common)
    if rcfg.get("news_computer"):
        prepare = {**prepare, "computer": rcfg["news_computer"]}
    try:
        pc = lib_store.get(prepare["computer"])
    except lib_store.LibraryNotFound:
        flow["error"] = "시작 전 준비의 회사 컴퓨터를 찾을 수 없습니다."
        return False
    args = _prepare_args(prepare, None, common)
    if rcfg.get("news_queries") and "queries" in args:   # 의뢰 구역·업종에 맞춘 검색어(시 전체는 지역 기본 검색어)
        args["queries"] = "\n".join(regions.news_queries(rcfg, common.get("area"), common.get("upjong")))
    rec = {"computer": pc["id"], "computer_name": pc["name"], "tool": prepare["tool"], "args": args, "job": None, "state": "waiting", "note": ""}
    flow["prepare"] = rec
    try:
        job = collect.request(pc["id"], prepare["tool"], [args], requested_by=f"{flow.get('office_name', '')} 흐름 {flow['id']}",
                              reason="흐름 시작 전 준비")[0]
    except collect.CollectError as e:
        rec.update(state="failed", note=str(e)[:300])
        flow["error"] = f"시작 전 준비를 못 했습니다: {e}"
        _save(flow)
        return False
    rec["job"] = job["id"]
    while True:
        j = next((x for x in collect.load_jobs(pc["id"]) if x["id"] == job["id"]), None)
        if j is None:
            rec.update(state="failed", note="작업 기록이 없습니다")
        elif j["status"] in collect.ACTIVE:
            rec.update(state="waiting", note="받는 중" if j["status"] == "running" else "대기")
        elif j["status"] == "failed":
            rec.update(state="failed", note="; ".join(j.get("notes") or []) or "실패")
        else:
            docs = j.get("documents") or []
            states = [(rag_store.get_document(pc["id"], d["doc_id"]) or {}).get("status") for d in docs]
            busy = sum(1 for st in states if st in rag_store.PROCESSING)
            if busy:
                rec.update(state="waiting", note=f"색인 중 {busy}/{len(states)}건")
            else:
                rec.update(state="ready", note=f"새 문서 {len(states)}건" + ("; " + "; ".join(j.get("notes") or [])[:200] if j.get("notes") else ""))
        flow["stage"] = f"시작 전 준비: {pc['name']} — {rec['note']}"
        _save(flow)
        if rec["state"] != "waiting":
            break
        with _lock:
            if _active.get(flow["office_id"]) != flow["id"] or flow["id"] in _stopping:
                return False
        time.sleep(COMPUTER_POLL)
    if rec["state"] != "ready":
        flow["error"] = f"시작 전 준비 실패: {rec['note']}"
        return False
    return True


def _run_repeat_department(flow: dict, dep: dict, common: dict, desk_keys: set[str] | None = None) -> bool:
    """반복 부서: 출처 책상 결과의 배열(예: candidates)에서 한 항목씩, 같은 캐릭터·업무를 항목마다 새 컨텍스트로 부른다
    (앞 부서 실행 전엔 배열 길이를 모르므로 책상을 여기서 만든다). 동시 실행 수는 repeat.max_concurrent(기본 3).
    항목 하나가 실패해도 "not_covered"로 기록하고 나머지는 계속 돈다 — 부서 자체는 막지 않는다.
    desk_keys 가 있으면("이 책상만 다시") 이미 만들어져 있는 항목 목록에서 그 책상들만 다시 실행한다(전체를 새로 안 만든다)."""
    dep["status"] = "running"
    rc = dep["repeat"]

    if desk_keys is not None and dep["desks"]:   # 이미 항목 목록이 있다 — 다시 만들지 않고 그 중 지정된 것만 다시 돈다
        for x in dep["desks"]:
            if x["key"] in desk_keys:
                x.update(status="pending", stage="", run_id=None, error=None, duration_s=None, usage=None, tool_calls=None, input_chars={}, warnings=None)
                flow["outputs"].pop(x["key"], None)
        agent_id, task_id = dep["desks"][0]["agent_id"], dep["desks"][0]["task_id"]
        try:
            agent = agent_store.get(agent_id)
            task = runs.find_task(agent, task_id)
        except (agent_store.AgentNotFound, runs.RunError) as e:
            dep["status"] = "failed"
            flow["error"] = f"{dep['name']}: 반복할 캐릭터·업무를 찾을 수 없습니다: {e}"
            _save(flow)
            return False
        _save(flow)
    else:
        flow["stage"] = f"{dep['name']} 실행 준비 중(반복할 목록 확인)"
        _save(flow)
        src_data = flow["outputs"].get(rc["source"])
        if src_data is None:
            dep["status"] = "failed"
            flow["error"] = f"{dep['name']}: 반복할 목록의 출처({rc['source']}) 결과가 없습니다."
            _save(flow)
            return False
        items = src_data.get(rc["field"]) if isinstance(src_data, dict) else None
        if not isinstance(items, list):
            dep["status"] = "failed"
            flow["error"] = f"{dep['name']}: {rc['source']} 결과의 '{rc['field']}'가 배열이 아닙니다."
            _save(flow)
            return False

        try:
            agent = agent_store.get(rc["template_agent_id"])
            task = runs.find_task(agent, rc["template_task_id"])
        except (agent_store.AgentNotFound, runs.RunError) as e:
            dep["status"] = "failed"
            flow["error"] = f"{dep['name']}: 반복할 캐릭터·업무를 찾을 수 없습니다: {e}"
            _save(flow)
            return False

        if not items:
            dep["status"] = "ok"   # 반복할 항목이 0개면 빈 채로 통과 — 다음 부서가 빈 목록으로 처리하게 둔다
            flow["outputs"][dep["id"]] = {"covered": 0, "not_covered": 0, "items": []}
            _save(flow)
            return True

        # 배열 항목 하나에 출처 결과의 최상위 키를 같이 넣는다(예: favored_traits). 설정 전에 시작한 흐름을 다시 돌릴 때는
        # 준비 설정(_current_prepare)과 같이 지금 회사의 흐름 설정에서 가져온다(2026-09-28).
        merge_keys = rc.get("merge_from_root") or _current_repeat(flow, dep).get("merge_from_root") or []
        if merge_keys and isinstance(src_data, dict):
            items = [{**it, **{k: src_data.get(k) for k in merge_keys if k in src_data}} if isinstance(it, dict) else it for it in items]

        dep["desks"] = [{"key": f"{dep['id']}:{i}", "agent_id": agent["id"], "agent_name": agent["name"], "agent_role": agent["role"],
                         "task_id": task["id"], "task_name": task["name"], "status": "pending", "stage": "", "run_id": None, "error": None,
                         "sources": {}, "input_conn": {}, "input_chars": {}, "duration_s": None, "usage": None, "tool_calls": None,
                         "repeat_index": i, "repeat_item": items[i]}
                        for i in range(len(items))]

    flow["stage"] = f"{dep['name']} 실행 중"
    _save(flow)
    targets = [x for x in dep["desks"] if desk_keys is None or x["key"] in desk_keys]

    # 시작 전 준비(흐름 설정): 결재를 올리기 전에 회사 컴퓨터가 항목마다 자료를 받아 두고, 준비될 때까지 기다린다.
    prepare = rc.get("prepare") or _current_prepare(flow, dep)
    if prepare:
        targets = _wait_for_computer(flow, dep, targets, common, prepare)
        if targets is None:
            return False   # 기다리는 중에 흐름이 멈췄다

    max_conc = max(1, min(REPEAT_MAX_CONCURRENT, rc.get("max_concurrent") or REPEAT_DEFAULT_CONCURRENT))
    sem = threading.Semaphore(max_conc)

    def stopped() -> bool:
        with _lock:
            return _active.get(flow["office_id"]) != flow["id"]

    def run_one(x: dict) -> None:
        with sem:
            if stopped():   # 정지된 흐름은 남은 항목의 결재를 새로 올리지 않는다
                return
            try:
                values, _long_names, input_chars = _desk_inputs(flow, agent, task, x, common)
                values[rc["input_name"]] = _stringify(x["repeat_item"])
                x["input_chars"] = input_chars
                # 결재 관문: 프롬프트를 만들어 두고(모델 안 부름) 승인될 때까지 기다린다. 승인 전에는 전송 안 함.
                _stage_for_approval(flow, x, agent, task, values)
                _save(flow)
                if not _wait_for_approval(flow, x):   # 거부되었거나 흐름이 멈춤
                    _save(flow)
                    return
                x["status"], x["stage"] = "running", "대기"
                _save(flow)
                run = runs.start(agent, task, values, {rc["input_name"]}, track_busy=False)   # 같은 캐릭터가 항목마다 동시에 돈다 — "바쁨" 표시로 서로 막지 않는다
                x["run_id"], x["status"], x["stage"] = run["id"], "running", run.get("stage") or "대기"
                _save(flow)
            except (runs.RunError, agent_store.AgentNotFound) as e:
                x["status"], x["error"] = "not_covered", f"시작하지 못했습니다: {e}"
                _save(flow)
                return
            while True:
                try:
                    r = runs.get(x["agent_id"], x["run_id"])
                except runs.RunError:
                    break
                x["stage"] = r.get("stage") or ""
                if r.get("status") not in ("running", "paused"):
                    break
                x["status"] = "paused" if r.get("status") == "paused" else "running"
                _save(flow)
                time.sleep(POLL_SECONDS)
            _record_repeat_result(flow, x, runs.get(x["agent_id"], x["run_id"]))
            _save(flow)

    def run_one_safe(x: dict) -> None:
        try:
            run_one(x)
        except Exception:   # 저장 오류 등으로 스레드가 죽어도 실행 결과는 아래 '놓친 결과 거두기'가 기록한다
            log.exception("반복 부서 책상 스레드 오류 (%s) — 끝난 실행이면 부서 마무리 때 결과를 거둔다", x["key"])

    threads = [threading.Thread(target=run_one_safe, args=(x,), daemon=True) for x in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    _adopt_finished(flow, targets)
    if stopped():   # 결재 대기·실행 중에 정지됐다 — 부서를 '완료'로 넘기지 않는다(뒤 부서가 결재를 새로 만들지 않게)
        _save(flow)
        return False

    covered = sum(1 for x in dep["desks"] if x["status"] == "ok")
    flow["outputs"][dep["id"]] = {"covered": covered, "not_covered": len(dep["desks"]) - covered,
                                  "items": [{"index": x["repeat_index"], "status": x["status"],
                                            "result": flow["outputs"].get(x["key"])} for x in dep["desks"]]}
    dep["status"] = "ok"   # 반복은 항목 몇 개가 not_covered 여도 부서 자체는 막지 않는다(지시대로 "기록하고 계속")
    flow["totals"] = _totals(flow)
    _save(flow)
    return True


def _record_repeat_result(flow: dict, x: dict, r: dict) -> None:
    """끝난 실행(r)의 결과를 반복 부서 책상(x)에 기록한다: 성공이고 JSON 을 읽을 수 있으면 ok + 결과, 아니면 not_covered. 저장은 부르는 쪽이."""
    x["duration_s"], x["usage"] = r.get("duration_s"), r.get("usage")
    x["tool_calls"] = sum(1 for s in (r.get("sources") or []) if s.get("via") == "tool")
    x["stage"] = r.get("stage") or ""
    x["warnings"] = list(r.get("soft_pass") or [])
    if r.get("status") != "ok":
        x["status"], x["error"] = "not_covered", _fail_text(r)
        return
    data, _, err = checks.extract_json(r.get("answer") or "")
    if err:
        x["status"], x["error"] = "not_covered", f"결과에서 JSON 을 읽지 못했습니다: {err}"
        return
    try:
        _attach_evidence(flow, x, r, data)
    except Exception:
        log.exception("검증 근거 스냅샷 실패 (%s)", x["key"])
    flow["outputs"][x["key"]] = _with_hold(data, r, x)
    x["status"], x["error"] = "ok", None


def _adopt_finished(flow: dict, desks: list[dict]) -> int:
    """'실행 중'으로 멈춰 있지만 실제 실행은 이미 끝난 책상의 결과를 거둔다(스레드가 기록 전에 죽었거나 서버가 재시작된 경우).
    다시 돌리지 않고 끝난 실행 기록을 그대로 쓴다. 거둔 책상 수를 돌려준다(저장은 부르는 쪽이)."""
    n = 0
    for x in desks:
        if x.get("status") not in ("running", "paused", "interrupted") or not x.get("run_id"):
            continue
        try:
            r = runs.get(x["agent_id"], x["run_id"])
        except runs.RunError:
            continue
        if r.get("status") in ("running", "paused"):
            continue
        _record_repeat_result(flow, x, r)
        n += 1
        log.info("끝난 실행의 결과를 거뒀습니다 — 책상 %s, 실행 %s, 상태 %s", x["key"], x["run_id"], x["status"])
    return n


def _with_hold(data, run: dict, desk: dict):
    """자료 부족(data_missing)으로 '보류' 통과한 결과면 다음 단계에 표시한다: 넘기는 JSON 에 _hold(보류 사유)를 붙이고 책상 기록에도 남긴다
    (표준 v1.1 §5-3, 2026-09-26). 보류가 아니면 그대로."""
    hold = run.get("hold")
    if not hold:
        return data
    desk["hold"] = hold
    return {**data, "_hold": {"reason": "data_missing", "items": hold}} if isinstance(data, dict) else data


def _run_department(flow: dict, dep: dict, common: dict, desk_keys: set[str] | None = None) -> bool:
    """desk_keys 가 있으면 그 책상들만 (다시) 실행한다 — 나머지 책상은 이번 호출에서 손대지 않고 지금 상태(대개 이전 실행의 ok)를 그대로 쓴다.
    ("이 책상만 다시": 부서 전체가 아니라 실패한 책상 하나만 다시 돌리고, 같은 부서의 나머지 책상 결과는 재사용한다.)"""
    if dep.get("mode") == "repeat":
        return _run_repeat_department(flow, dep, common, desk_keys=desk_keys)
    dep["status"] = "running"
    flow["stage"] = f"{dep['name']} 실행 중"
    _save(flow)
    started = []
    targets = [x for x in dep["desks"] if desk_keys is None or x["key"] in desk_keys]

    # 1) 각 책상의 프롬프트를 만들어 '결재 대기'로 둔다(모델 안 부름). 전부 한꺼번에 목록에 뜬다.
    prepared = {}   # desk_key → (agent, task, values, long_names)
    for x in targets:
        try:
            agent = agent_store.get(x["agent_id"])
            task = runs.find_task(agent, x["task_id"])
            values, long_names, input_chars = _desk_inputs(flow, agent, task, x, common)
            x["input_chars"] = input_chars
            _stage_for_approval(flow, x, agent, task, values)
            prepared[x["key"]] = (agent, task, values, long_names)
        except (runs.RunError, agent_store.AgentNotFound) as e:
            x["status"], x["error"] = "failed", f"시작하지 못했습니다: {e}"
        except KeyError as e:
            x["status"], x["error"] = "failed", f"앞 단계 결과가 없습니다: {e}"
    _save(flow)

    # 2) 사람이 승인하는 대로 그 책상만 실제로 전송한다(개별 승인). 거부한 책상은 실패로 남긴다.
    pending = set(prepared)
    while pending:
        for key in list(pending):
            state = _approval_state(flow["id"], key)
            x = next(d for d in dep["desks"] if d["key"] == key)
            if state == "approved":
                agent, task, values, long_names = prepared[key]
                try:
                    run = runs.start(agent, task, values, long_names)
                    x["run_id"], x["status"], x["stage"] = run["id"], "running", run.get("stage") or "대기"
                    started.append(x)
                except (runs.RunError, agent_store.AgentNotFound) as e:
                    x["status"], x["error"] = "failed", f"시작하지 못했습니다: {e}"
                pending.discard(key)
            elif state == "rejected":
                x["status"], x["stage"], x["error"] = "failed", "결재 거부됨", "사람이 이 책상을 거부했습니다."
                pending.discard(key)
        with _lock:
            if _active.get(flow["office_id"]) != flow["id"]:   # 흐름이 멈췄다
                return False
        _save(flow)
        if pending:
            time.sleep(APPROVAL_POLL)

    while started:                                   # 부서의 모든 책상이 끝날 때까지 기다린다(동시 부서 = 전부 끝난 뒤에 다음 부서)
        with _lock:
            stopping = flow["id"] in _stopping
        if stopping:                                 # '작업 정지': 도는 책상은 다음 왕복이 취소되므로 곧 끝난다. 기다리지 않고 정지로 마감한다.
            for x in started:
                if x["status"] in ("running", "paused"):
                    x["status"], x["stage"], x["error"] = "stopped", "정지됨", "사용자가 작업을 정지시켰습니다."
            _save(flow)
            return False
        alive = False
        for x in started:
            try:
                r = runs.get(x["agent_id"], x["run_id"])
            except runs.RunError:
                continue
            x["stage"] = r.get("stage") or ""
            if r.get("status") in ("running", "paused"):   # paused: 출력 한도 도달, 사람이 "이어서 쓰기/중단"을 고를 때까지 기다린다
                alive = True
            x["status"] = "paused" if r.get("status") == "paused" else "running"
        _save(flow)
        if not alive:
            break
        time.sleep(POLL_SECONDS)

    for x in started:                                # 검증 관문: 성공이고 JSON 을 읽을 수 있는 결과만 통과
        r = runs.get(x["agent_id"], x["run_id"])
        x["duration_s"], x["usage"] = r.get("duration_s"), r.get("usage")
        x["tool_calls"] = sum(1 for s in (r.get("sources") or []) if s.get("via") == "tool")
        x["stage"] = r.get("stage") or ""
        x["warnings"] = list(r.get("soft_pass") or [])
        if r.get("status") != "ok":
            x["status"], x["error"] = "failed", _fail_text(r)
            continue
        data, _, err = checks.extract_json(r.get("answer") or "")
        if err:
            x["status"], x["error"] = "failed", f"결과에서 JSON 을 읽지 못했습니다: {err}"
            continue
        flow["outputs"][x["key"]] = _with_hold(data, r, x)
        x["status"], x["error"] = "ok", None
    ok = all(x["status"] == "ok" for x in dep["desks"])
    dep["status"] = "ok" if ok else "failed"
    flow["totals"] = _totals(flow)
    _save(flow)
    return ok
