"""업무 실행: 정해진 업무를 입력값과 함께 실행하고, 결과를 저장하고, 형식 검사에 실패하면 한 번 다시 시킨다.

- 실행은 백그라운드 스레드에서 돈다(리포트 하나에 도구를 여러 번 쓰면 몇 분 걸린다). 캐릭터 하나에 동시에 하나만 실행한다.
- 결과는 `agents/<id>/runs/<run_id>.json`에 저장한다(입력·답변·출처·도구 사용·검사 결과). 대화 기록(messages)과는 별개다.
- 검사(`task.check`, app/runs/checks.py)에 실패하면 실패 항목을 알려 주고 한 번(RUN_MAX_RETRIES) 다시 시킨다.
  JSON 을 못 읽은 경우에는 JSON 블록만 다시 받아 바꿔 끼우고(분석은 그대로), 내용 위반이면 실패 항목만 고쳐 전체를 다시 낸다.
- 입력 기본값: 입력 항목의 `default` 에 {today}(오늘) 또는 {last:항목이름}(이 업무의 마지막 성공 실행에서 그 항목에 넣은 값)을 쓸 수 있다.
  → 관측 기간처럼 "직전 종료일 = 이번 시작일"인 값을 사람이 다시 입력하지 않게 한다.
"""

import copy
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from app.agents import store
from app.chat import llm, pipeline, promptlog, usage
from app.chat import providers
from app.chat.providers import HardTruncated, Truncated
from app.runs import budget
from app.runs import cancel
from app.runs import checks, standard, verify
from app.runs import evidence
from app.runs import numbers
from app.runs import server_fill
from app.runs import soft
from app.tools import registry as _registry

log = logging.getLogger("agent_town.runs")

MAX_RETRIES = int(os.getenv("RUN_MAX_RETRIES") or 2)   # 업무별 retries 가 없을 때의 기본값
MAX_TOTAL_RETRIES = int(os.getenv("RUN_MAX_TOTAL_RETRIES") or 3)   # 실행 전체 재시도 상한(잘림 복구 + 형식 수정 합산 — 단계가 바뀌어도 초기화하지 않는다)
MAX_RUNS_KEPT = 200
MAX_CONTINUE = 2   # "이어서 쓰기"는 최대 이만큼(그 뒤로 또 잘리면 중단해야 한다)
FLOW_INPUT_MAX = int(os.getenv("FLOW_INPUT_MAX_CHARS") or 300000)   # 사무실 흐름이 앞 단계 결과 JSON 을 넘길 때의 입력 길이 한도(사람이 직접 넣는 값은 2000자)
RUN_ID = re.compile(r"^r_[0-9a-f]{8}$")
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_PLACEHOLDER2 = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")   # {{항목이름}}: 겹중괄호도 같은 뜻(입력 항목 값으로 바뀐다)

_active: dict[str, str] = {}    # agent_id → 지금 도는 run_id
_live: dict[str, dict] = {}     # run_id → 지금 도는 실행의 최신 상태(화면이 자주 조회하므로 파일이 아니라 여기서 읽는다)
_lock = threading.Lock()


class RunError(Exception):
    def __init__(self, message: str, code: int = 422):
        super().__init__(message)
        self.code = code


def _dir(agent_id: str):
    d = store.agent_dir(agent_id) / "runs"
    d.mkdir(exist_ok=True)
    return d


def _path(agent_id: str, run_id: str):
    if not RUN_ID.match(run_id):
        raise RunError("실행 기록을 찾을 수 없습니다", 404)
    return _dir(agent_id) / f"{run_id}.json"


def prompt_log(agent_id: str, run_id: str) -> str | None:
    """이 실행의 프롬프트 로그 파일 내용(promptlog 가 남긴 것). 없으면 None. 실행 중에도 열려 있어(flush) 읽을 수 있다."""
    if not RUN_ID.match(run_id):
        raise RunError("실행 기록을 찾을 수 없습니다", 404)
    p = _dir(agent_id) / f"{run_id}.prompt.log"
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save(run: dict) -> None:
    """실행 기록을 파일에 저장한다. 실행 중인 기록은 조회가 메모리(_live)에서 읽으므로, 파일을 바꿔 쓰는 순간 조회와 부딪히지 않는다.
    (윈도우에서는 누군가 읽고 있는 파일을 바꿔 쓰면 '접근 거부'가 난다 → 그래도 몇 번 다시 시도한다.)"""
    with _lock:
        _live[run["id"]] = copy.deepcopy(run)
    for attempt in range(6):
        try:
            store._write_json(_path(run["agent_id"], run["id"]), run)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.2)


def get(agent_id: str, run_id: str) -> dict:
    p = _path(agent_id, run_id)
    with _lock:
        live = _live.get(run_id)
    if live is not None:
        return copy.deepcopy(live)
    if not p.is_file():
        raise RunError("실행 기록을 찾을 수 없습니다", 404)
    return json.loads(p.read_text(encoding="utf-8"))


def summary(run: dict) -> dict:
    out = {k: run.get(k) for k in ("id", "task_id", "task_name", "inputs", "status", "stage", "started_at", "finished_at", "attempts", "error", "model", "usage", "duration_s")} | {
        "check_ok": (run.get("check") or {}).get("ok"), "has_check": run.get("has_check", False), "chars": len(run.get("answer") or "")}
    if run.get("pause"):
        out["pause"] = _pause_info(run["pause"])
    return out


def list_runs(agent_id: str, task_id: str | None = None, limit: int = 50) -> list[dict]:
    out = []
    for p in _dir(agent_id).glob("r_*.json"):
        with _lock:
            r = copy.deepcopy(_live.get(p.stem))
        try:
            r = r or json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if task_id is None or r.get("task_id") == task_id:
            out.append(r)
    out.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    return [summary(r) for r in out[:limit]]


def delete(agent_id: str, run_id: str) -> None:
    with _lock:
        if _active.get(agent_id) == run_id:
            raise RunError("실행 중인 기록은 지울 수 없습니다", 409)
    p = _path(agent_id, run_id)
    if not p.is_file():
        raise RunError("실행 기록을 찾을 수 없습니다", 404)
    p.unlink()


# ---------------------------------------------------------------- 입력

def find_task(agent: dict, task_id: str) -> dict:
    for t in agent["work"]["tasks"]:
        if t.get("id") == task_id:
            return t
    raise RunError("업무를 찾을 수 없습니다", 404)


def defaults(agent: dict, task: dict) -> dict:
    """입력 항목별 기본값. {today}, {last:항목이름} 을 풀어서 돌려준다(못 풀면 빈 글)."""
    last = None
    for r in list_runs(agent["id"], task["id"], limit=MAX_RUNS_KEPT):
        if r["status"] in ("ok", "warning"):
            last = r["inputs"]
            break
    today = datetime.now().astimezone().strftime("%Y-%m-%d")

    def fill(m):
        key = m.group(1).strip()
        if key == "today":
            return today
        if key.startswith("last:"):
            return str((last or {}).get(key[5:].strip(), ""))
        return m.group(0)

    return {i["name"]: _PLACEHOLDER.sub(fill, i.get("default") or "").strip() for i in task["inputs"]}


def clean_inputs(task: dict, raw: dict, long_names=()) -> dict:
    if not isinstance(raw, dict):
        raise RunError("입력값 형식이 올바르지 않습니다")
    known = {i["name"] for i in task["inputs"]}
    extra = [k for k in raw if k not in known]
    if extra:
        raise RunError(f"이 업무에 없는 입력 항목입니다: {extra[0]}")
    out = {}
    for i in task["inputs"]:
        v = str(raw.get(i["name"], "") if raw.get(i["name"]) is not None else "").strip()
        if not v:
            if i["required"]:
                raise RunError(f"'{i['name']}'을(를) 입력해 주세요")
            out[i["name"]] = ""
            continue
        limit = FLOW_INPUT_MAX if (i["name"] in long_names or i["type"] == "json") else 2000
        if len(v) > limit:
            raise RunError(f"'{i['name']}'이(가) 너무 깁니다({limit}자 이내)")
        if i["type"] == "json":
            try:
                json.loads(v)
            except ValueError as e:
                raise RunError(f"'{i['name']}'은(는) 올바른 JSON 이어야 합니다({str(e)[:80]})")
        if i["type"] == "number":
            try:
                float(v)
            except ValueError:
                raise RunError(f"'{i['name']}'은(는) 숫자여야 합니다")
        if i["type"] == "choice" and v not in i["options"]:
            raise RunError(f"'{i['name']}'은(는) {', '.join(i['options'])} 중 하나여야 합니다")
        out[i["name"]] = v
    return out


def render(task: dict, values: dict) -> str:
    """수행 지시의 {항목}을 입력값으로 바꾼 글. 이것이 이번 실행의 사용자 메시지가 된다."""
    swap = lambda m: values[m.group(1).strip()] if m.group(1).strip() in values else m.group(0)
    # {{thresholds.이름}}·{{version}} 은 표준 설정값으로(수행 지시에 숫자를 복사하지 않기 위해, 표준 v1.1 §7)
    return standard.render_text(_PLACEHOLDER.sub(swap, _PLACEHOLDER2.sub(swap, task["instruction"])))


# ---------------------------------------------------------------- 실행

def start(agent: dict, task: dict, raw_inputs: dict, long_inputs=(), track_busy: bool = True) -> dict:
    """long_inputs: 사무실 흐름이 앞 단계 결과 JSON 을 넣는 입력 항목 이름(길이 한도가 크다). 기록에는 길이만 남겨 목록 조회가 무거워지지 않게 한다.
    track_busy=False: "이 캐릭터는 바쁘다" 표시·다른 실행 막기를 안 한다 — 반복 부서(같은 캐릭터가 항목마다 동시에 여러 번
    실행됨, app/offices/flow.py)전용. 그 밖엔 항상 True(캐릭터 하나는 한 번에 한 실행만)."""
    missing_tools = [t for t in (task.get("required_tools") or []) if t not in (agent.get("tools") or [])]
    if missing_tools:
        raise RunError(f"이 업무에 필요한 도구가 캐릭터에서 꺼져 있어 실행을 시작하지 않았습니다: {missing_tools}. "
                        "캐릭터 수정 → 도구 탭에서 켜세요.", 422)
    long_inputs = set(long_inputs) | {i["name"] for i in task["inputs"] if i["type"] == "json"}
    inputs = clean_inputs(task, raw_inputs, long_inputs)
    shown = {k: (f"(앞 단계 결과 {len(v):,}자)" if k in long_inputs and len(v) > 2000 else v) for k, v in inputs.items()}
    with _lock:
        if track_busy and agent["id"] in _active:
            raise RunError("이 캐릭터가 이미 다른 업무를 실행 중입니다. 끝난 뒤에 다시 실행하세요.", 409)
        run_id = "r_" + secrets.token_hex(4)
        if track_busy:
            _active[agent["id"]] = run_id
    try:
        if len(list(_dir(agent["id"]).glob("r_*.json"))) >= MAX_RUNS_KEPT:
            raise RunError(f"실행 기록이 {MAX_RUNS_KEPT}개입니다. 오래된 기록을 지운 뒤 실행하세요.", 409)
        run = {"id": run_id, "agent_id": agent["id"], "task_id": task["id"], "task_name": task["name"], "inputs": shown,
               "status": "running", "stage": "대기", "started_at": _now(), "finished_at": None, "model": agent["model"],
               "has_check": verify.has_checks(task, agent["id"]), "usage": None, "duration_s": None, "attempts": [], "answer": "", "sources": [], "notice": None, "check": None, "error": None,
               "task_snapshot": task}
        _save(run)
    except Exception:
        with _lock:
            if track_busy:
                _active.pop(agent["id"], None)
        raise
    threading.Thread(target=_execute, args=(agent, task, run, inputs), kwargs={"track_busy": track_busy}, name=f"run-{run_id}", daemon=True).start()
    return run


def _stage(run: dict, text: str) -> None:
    run["stage"] = text
    _save(run)


def _fix_message(problems: list[str], parse_failed: bool) -> str:
    """검증에 실패했을 때 모델에게 보내는 재요청. 분석 전체가 아니라 JSON 블록만 다시 내게 한다(분석 내용이 달라지지 않게)."""
    listed = "\n".join(f"- {p}" for p in problems)
    head = "출력 JSON을 읽을 수 없었습니다." if parse_failed else "출력 JSON이 검증에 실패했습니다."
    return (head + "\n\n오류:\n" + listed + "\n\n"
            "분석 내용과 JSON 밖의 본문은 그대로 두고, JSON 블록만 수정해서 ```json 코드펜스 안에 다시 출력하세요. 다른 설명은 붙이지 마세요.")


_COMPONENT_ORDER = ["macro", "policy", "geo", "sector", "quality"]
_REPORT_LABELS = {"macro": "거시", "policy": "정책", "geo": "지정학", "sector": "산업"}
def _decomposition_table_md(cands: list) -> str | None:
    """candidates[] 마다 components(점수 분해: macro/policy/geo/sector/quality 등)가 있으면 표로 만든다.
    이 모양이 아니면(팀장처럼 여러 성분 합으로 점수를 매기는 업무가 아니면) None — 아무것도 하지 않는다."""
    rows = []
    for c in cands:
        if not isinstance(c, dict):
            return None
        comp = c.get("components")
        if not isinstance(comp, dict) or not comp:
            return None   # 이 업무의 후보 모양이 다르면(점수 분해가 없으면) 손대지 않는다
        rows.append((c.get("rank"), c.get("ticker"), c.get("gics_industry"), c.get("score"), comp))
    all_keys = {k for *_, comp in rows for k in comp}
    comp_keys = [k for k in _COMPONENT_ORDER if k in all_keys] + sorted(all_keys - set(_COMPONENT_ORDER))
    header = "| 순위 | 티커 | 산업 | 총점 | " + " | ".join(comp_keys) + " |"
    sep = "|" + "---|" * (4 + len(comp_keys))
    lines = [header, sep]
    for rank, ticker, gics, score, comp in sorted(rows, key=lambda r: (r[0] if isinstance(r[0], (int, float)) else 999)):
        vals = " | ".join(f"{comp[k]:.4f}" if isinstance(comp.get(k), (int, float)) else "" for k in comp_keys)
        score_s = f"{score:.4f}" if isinstance(score, (int, float)) else ""
        lines.append(f"| {rank if rank is not None else ''} | {ticker or ''} | {gics or ''} | {score_s} | {vals} |")
    return ("\n\n## 점수 분해 표 (서버가 도구 결과에서 그대로 뽑아 붙였습니다 — 팀장이 쓴 것이 아닙니다)\n\n"
            + "\n".join(lines) + "\n")


def _augment_answer(text: str) -> str:
    """모델에게 다시 쓰게 하지 않고, 서버가 이미 계산된 값을 그대로 표로 만들어 결과 끝에 붙인다.
    candidates[].components 모양이 아니면(팀장처럼 여러 성분 합으로 점수를 매기는 업무가 아니면) 아무것도 하지 않는다.
    업무 지시·스키마는 그대로 둔다 — 이 표들은 화면·다운로드에만 남고, 팀장의 다음 입력에는 들어가지 않는다."""
    data, _, err = checks.extract_json(text)
    if err or not isinstance(data, dict):
        return text
    cands = data.get("candidates")
    if not isinstance(cands, list) or not cands:
        return text
    decomp = _decomposition_table_md(cands)
    if decomp is None:
        return text
    return text.rstrip() + decomp


def _verify_execution(task: dict, text: str, agent_id: str, run: dict) -> dict:
    # 실행 기록(도구 결과)을 넘겨 공통 검증이 '확인 여부'를 실제로 연 자료와 대조하게 한다(표준 v1.1 §5)
    chk = verify.run(task, text, agent_id, sources=run.get("sources") or [])
    issues = evidence.problems(task, text, run.get("sources") or [])
    if task.get("number_binding"):   # 업무 설정: 숫자는 변수로(app/runs/numbers.py) — 근거 조각과 대조, 어긋나면 재요청
        nums = numbers.problems(text, run)
        if nums:
            member = standard.is_member(agent_id)
            chk["ok"] = False
            chk["problems"] += [standard.with_code(p, "citation_error") if member else p for p in nums]
            chk["kinds"] = sorted(set(chk["kinds"]) | {"content"})
            if member:
                chk["codes"] = sorted(set(chk.get("codes") or []) | {"citation_error"})
    if task.get("quote_source") == "documents":   # 업무 설정: 인용은 이번 실행에서 연 문서 원문 그대로여야 함(어긋나면 내용 오류)
        quotes = server_fill.quote_problems(text, run)
        if quotes:
            member = standard.is_member(agent_id)
            chk["ok"] = False
            chk["problems"] += [standard.with_code(p, "citation_error") if member else p for p in quotes]
            chk["kinds"] = sorted(set(chk["kinds"]) | {"content"})
            if member:
                chk["codes"] = sorted(set(chk.get("codes") or []) | {"citation_error"})
    if issues:
        if standard.is_member(agent_id):   # 플랫폼 검사 오류에도 분류 코드(필수 도구 실패 → tool_failure, 전문자료 없음 → data_missing …)
            issues = [standard.with_code(p, standard.platform_code(p)) for p in issues]
            chk["codes"] = sorted(set(chk.get("codes") or []) | {standard.code_of(p) for p in issues})
        chk["ok"] = False
        chk["problems"] += issues
        chk["kinds"] = sorted(set(chk["kinds"]) | {"evidence"})
    return chk


def _codes(chk: dict) -> set[str]:
    """검사 결과의 분류 코드 — 전체 문제에서 센 codes 가 있으면 그것(보이는 20건 요약 줄 제외), 없으면 보이는 문제에서."""
    if chk.get("codes") is not None:
        return set(chk["codes"])
    return {standard.code_of(p) for p in chk["problems"] if not str(p).startswith("… 외 ")}


def _retryable(chk: dict, member: bool) -> bool:
    """이 검사 결과로 'JSON 만 다시' 요청을 할 것인가. 표준 적용 대상은 코드로 가른다(표준 v1.1 §5-3):
    재요청 코드(format·calc_error·citation_error·policy_violation)가 있고, 실행 실패(tool_failure)·멈춤(legacy_unclassified) 코드가 없을 때만.
    그 밖의 직원은 예전 그대로(훅 고장만 빼고 재요청)."""
    if chk["ok"] or "hook_error" in chk["kinds"]:
        return False
    if soft.all_soft(chk["problems"]):   # 남은 문제가 모두 경고 통과 대상이면 모델을 다시 부르지 않는다(비용만 든다)
        return False
    if not member:
        return True
    codes = _codes(chk)
    return bool(codes & standard.RETRY_CODES) and not (codes & (standard.FAIL_CODES | standard.STOP_CODES))


def _check_and_status(task: dict, run: dict, text: str, chk: dict, label: str = "") -> None:
    num_warns: list[str] = []
    if task.get("number_binding"):   # 숫자 자리표시자 채우기(확인 못 한 변수는 〔확인 안 됨〕) — 서버 채우기보다 먼저
        text, num_warns = numbers.apply(text, run)
    text, fill_warns = server_fill.apply(text, run)   # 작성 시각·제출일은 서버가 채우고 인용은 원문과 대조(검증 뒤라 판정엔 영향 없음)
    text = _augment_answer(text)
    warnings = list(chk.get("warnings") or []) + num_warns + fill_warns
    if not chk["ok"] and "hook_error" not in chk["kinds"] and soft.all_soft(chk["problems"], final=True):
        # 경고 통과(허용 목록: 서버가 채우는 칸의 null·상한 이하 인용 길이). 결과는 살리고, 경고로 남기고, 건수를 로그에 쓴다
        items = [soft.label(p) for p in chk["problems"]]
        run["soft_pass"] = items
        warnings += items
        chk = {**chk, "ok": True, "problems": []}
        label = f"(경고 {len(items)}건){label}"
        log.info("경고 통과 run=%s agent=%s 건수=%d: %s", run.get("id"), run.get("agent_id"), len(items), " / ".join(items)[:500])
    if standard.is_member(run.get("agent_id")):
        # 글 부분(JSON 밖)의 검색 결과 번호([W4] 등)는 산출물에 남기지 않는다(표준 2-8). JSON 안의 것은 검증에서 오류로 잡는다.
        text, n = standard.strip_prose_footnotes(text)
        if n:
            warnings.append(f"[policy_violation] 글 부분의 검색 결과 번호 {n}개를 지웠습니다.")
    run["answer"] = text
    run["check"] = {"ok": chk["ok"], "problems": chk["problems"], "warnings": warnings}
    codes = _codes(chk) if (not chk["ok"] and standard.is_member(run.get("agent_id"))) else set()
    if chk["ok"]:
        run["status"], run["stage"] = "ok", f"완료{label}"
    elif codes & standard.FAIL_CODES:
        # 도구 실패: 도구 호출은 이미 2회 더 시도했다(runner). 그래도 필수 도구가 실패했으면 실행 실패(재요청하지 않음)
        run["status"], run["stage"] = "error", f"실행 실패(도구 실패){label}"
        run["error"] = next(p for p in chk["problems"] if standard.code_of(p) in standard.FAIL_CODES)
    elif codes & standard.STOP_CODES:
        run["status"], run["stage"] = "check_failed", f"검사 실패(분류 안 된 오류 — 재요청 없이 멈추고 기록){label}"
    elif codes and codes <= standard.HOLD_CODES:
        # 자료 부족만 남음: 재요청 없이 '보류'로 통과시키고, 다음 단계에 보류 사유를 넘긴다(flow 가 결과 JSON 에 _hold 로 붙임)
        run["status"], run["stage"] = "ok", f"완료(보류: 자료 부족 {len(chk['problems'])}건){label}"
        run["hold"] = list(chk["problems"])
    elif task.get("on_failure") == "warn" and "evidence" not in chk["kinds"]:
        run["status"], run["stage"] = "warning", f"검사 실패 — 설정에 따라 경고만 하고 저장{label}"
    else:
        run["status"], run["stage"] = "check_failed", f"검사 실패(사람이 확인 필요){label}"


def _pause_info(pause: dict) -> dict:
    """화면에 보여줄 만큼만: session_state(대화 전문)는 뺀다."""
    return {k: v for k, v in pause.items() if k != "session_state"}


def _trunc_note(e) -> str:
    applied = getattr(e, "applied", None)
    s = f"출력이 최대 길이({applied}토큰)에 도달해 답변이 잘렸습니다" if applied else "출력이 최대 길이에 도달해 답변이 잘렸습니다"
    if applied == providers.FALLBACK_MAX_TOKENS:
        s += f" (주의: 출력 한도가 {providers.FALLBACK_MAX_TOKENS}로 낮아진 상태 — 작성 여유가 크게 줄었습니다)"
    return s


def _rewrite_from_draft(agent_id: str, agent: dict, base_messages: list[dict], draft: str, values: dict, task: dict, run: dict) -> str | None:
    """잘림 복구: '검색 없이' 초안에서 한도 안에 들어오게 한 번만 다시 쓴다(§5). 완성하면 텍스트, 또 잘리거나 JSON 없으면 None."""
    instr = ("아래는 출력 한도에 걸려 잘린 초안입니다. 새로 검색하지 말고, 이미 확보한 내용만으로 한도 안에서 처음부터 "
             "간결하게 다시 써서 필수 JSON 블록까지 완성하세요. 반복 설명과 중복 표를 줄이고, 근거·핵심 판단·필수 JSON 필드를 먼저 채우세요.")
    follow = base_messages + [{"role": "assistant", "content": draft}, {"role": "user", "content": instr}]
    try:
        res = pipeline.answer(agent_id, agent, follow, mode="task", retrieval_messages=base_messages, inputs=values,
                              max_tokens=task.get("max_output_tokens"), required_tools=task.get("required_tools"),
                              cancel_run_id=run["id"], use_tools=False)
    except HardTruncated:
        return None
    if res.get("notice"):
        run["notice"] = res["notice"]
    run["sources"] += res.get("sources", [])
    new, _s, _e, _err = checks.locate_json(res["answer"])
    return res["answer"] if new is not None else None


def _execute(agent: dict, task: dict, run: dict, values: dict | None = None, track_busy: bool = True) -> None:
    agent_id = agent["id"]
    box = usage.start()
    t0 = time.time()
    m = agent.get("model") or {}
    promptlog.start(_dir(agent_id) / f"{run['id']}.prompt.log", agent_name=agent.get("name", ""), agent_id=agent_id,
                    task_name=task.get("name", ""), provider=m.get("provider", ""), model=m.get("model", ""))
    if standard.is_member(agent_id):
        run["standard"] = standard.version_info()   # 이 실행에 적용된 표준 버전과 설정·공통 규칙 파일 지문(표준 v1.1 §7)
    try:
        render_values = values if values is not None else run["inputs"]      # 도구 인자의 「@입력이름」 도 이 값으로 바뀐다
        trigger = render(task, render_values)
        messages = [{"role": "user", "content": trigger}]
        # 단계별 조사: 실행 전체 검색 예산을 설정한다(업무별 search_budget, 없으면 기본 60). 소진되면 tool_chat 이 검색 도구를 끈다.
        search_budget = task.get("search_budget") or _registry.DEFAULT_SEARCH_BUDGET
        budget.set_budget(run["id"], search_budget)
        # 대화 전체 글자 예산(§6). 작성 여유는 이 업무의 출력 한도로 계산한다(21,000토큰 → 약 6.1만 자, 최소 3만 자).
        budget.set_context_budget(run["id"], task.get("context_budget") or _registry.DEFAULT_CONTEXT_SOFT_CHARS,
                                  reserve=budget.write_reserve_chars(task.get("max_output_tokens")))
        _stage(run, "자료 검색·답변 작성 중")
        used = 0   # 실행 전체 재시도 횟수(잘림 복구 + 형식 수정 합산 — 단계가 바뀌어도 초기화하지 않는다, §5)
        pass1_history = None   # 1차 패스의 대화 이력(도구 호출·결과 포함) — 재시도가 이어받는다(#1: 관측 유실 방지)
        try:
            res = pipeline.answer(agent_id, agent, messages, mode="task", inputs=render_values, max_tokens=task.get("max_output_tokens"), required_tools=task.get("required_tools"), cancel_run_id=run["id"], search_budget=search_budget)
            text, run["sources"], run["notice"] = res["answer"], res["sources"], res["notice"]
            pass1_history = res.get("history")
        except HardTruncated as e:
            # 잘림: 정상 완료로 저장하지 않는다. 검색 없이 최대 1회 재작성(전체 예산에서 1 차감).
            run["sources"] += [{**s, "via": "tool"} for s in e.trace]
            run["attempts"].append({"n": 1, "ok": False, "problems": [_trunc_note(e)], "mode": "잘림"})
            used += 1
            _stage(run, "출력 잘림 → 검색 없이 1회 재작성 중")
            text = _rewrite_from_draft(agent_id, agent, messages, e.text, render_values, task, run)
            if text is None:   # 재작성도 잘리거나 JSON 없음 → 미완료(초안·오류 보존)
                run["answer"] = e.text
                run["status"], run["stage"], run["error"] = "error", "미완료(잘림)", _trunc_note(e) + " 검색 없이 1회 재작성했으나 완성하지 못해 미완료로 종료합니다(초안 보존)."
                run["attempts"].append({"n": 2, "ok": False, "problems": ["재작성 후에도 미완성(잘림 또는 JSON 없음)"], "mode": "잘림 재작성"})
                return
            run["attempts"].append({"n": 2, "ok": True, "problems": [], "mode": "잘림 재작성", "note": "검색 없이 초안에서 재작성"})

        _stage(run, "형식 검사 중")
        chk = _verify_execution(task, text, agent_id, run)
        run["attempts"].append({"n": len(run["attempts"]) + 1, "ok": chk["ok"], "problems": chk["problems"]})

        task_retries = task["retries"] if task.get("retries") is not None else MAX_RETRIES
        retry_budget = min(task_retries, MAX_TOTAL_RETRIES)   # 형식 수정 + 잘림 복구를 합친 전체 상한
        member = standard.is_member(agent_id)
        # 훅 자체가 고장난 경우(hook_error)는 모델 잘못이 아니므로 다시 시키지 않는다. 표준 적용 대상은 오류 코드로 재요청 여부를 가른다.
        while _retryable(chk, member) and used < retry_budget:
            used += 1
            _stage(run, f"검사 실패 → JSON만 다시 받는 중({used}/{retry_budget})")
            # #1: 1차 패스에서 모은 도구 관측(관보·무역통계·검색)을 그대로 이어붙여, 재시도가 그 근거로 JSON 을 고치게 한다.
            # (검색 도구는 use_tools=False 로 꺼서 재검색은 막고, 이미 받은 관측만 유지.) 이력이 없으면(공급사 미지원) 기존 방식으로.
            base = pass1_history if pass1_history else messages
            # 자료 부족(data_missing)은 재요청으로 고칠 수 없고, 알려 주면 없는 자료를 지어낼 수 있어 재요청 문구에서 뺀다
            fix = [p for p in chk["problems"] if not (member and standard.code_of(p) in standard.HOLD_CODES and not str(p).startswith("… 외 "))]
            follow = base + [{"role": "assistant", "content": text}, {"role": "user", "content": _fix_message(fix, chk["kinds"] == ["parse"])}]
            try:
                res2 = pipeline.answer(agent_id, agent, follow, mode="task", retrieval_messages=messages, inputs=render_values, max_tokens=task.get("max_output_tokens"), required_tools=task.get("required_tools"), cancel_run_id=run["id"], use_tools=False)
            except HardTruncated as e:
                # 형식 수정 중에도 잘림 → 추가 복구 없이 미완료로 종료(전체 상한 취지: 순환 방지, §5)
                run["answer"] = text
                run["status"], run["stage"], run["error"] = "error", "미완료(잘림)", "형식 수정 중 출력이 다시 잘려 미완료로 종료합니다(초안 보존)."
                run["attempts"].append({"n": len(run["attempts"]) + 1, "ok": False, "problems": ["형식 수정 중 잘림"], "mode": "JSON만 다시"})
                return
            run["sources"] += res2["sources"]
            if res2.get("notice"):
                run["notice"] = res2["notice"]
            new, start, end, err = checks.locate_json(res2["answer"])
            note = None
            if new is None:
                note = f"재요청 응답에서 JSON 을 얻지 못했습니다: {err}"      # 이전 답변을 그대로 두고 다시 시도한다
            else:
                text = checks.replace_json(text, res2["answer"][start:end])
            _stage(run, "형식 검사 중")
            chk = _verify_execution(task, text, agent_id, run)
            att = {"n": len(run["attempts"]) + 1, "ok": chk["ok"], "problems": chk["problems"], "mode": "JSON만 다시"}
            if note:
                att["note"] = note
            run["attempts"].append(att)

        _check_and_status(task, run, text, chk)
    except Truncated as e:
        # 출력 한도 도달(도구 호출 없이 마지막 글을 쓰던 중). 처음부터 다시 시키면 도구 호출까지 전부 반복돼 비용이 크므로,
        # 사람이 "이어서 쓰기/중단"을 고를 때까지 멈춘다(나중에 만들 승인 관문과 같은 멈춤→승인→재개 방식).
        run["sources"] += e.trace
        run["pause"] = {"reason": "max_tokens", "text_so_far": e.text, "chars_so_far": len(e.text),
                        "continued": 0, "usage_last": e.resp_usage, "session_state": e.session_state, "paused_at": _now()}
        run["status"], run["stage"] = "paused", f"승인 대기: 출력 한도 도달 ({len(e.text):,}자 씀)"
    except cancel.Cancelled:
        # '작업 정지': 다음 모델 왕복을 하지 않고 여기서 멈춘다. 지금까지 쓴 글은 버린다(부분 결과는 검증도 못 통과한다).
        run["status"], run["stage"], run["error"] = "stopped", "정지됨", "사용자가 작업을 정지시켰습니다."
    except llm.ChatError as e:
        run["status"], run["stage"], run["error"] = "error", "오류", str(e)
    except Exception as e:
        log.exception("업무 실행 오류 (%s)", agent_id)
        run["status"], run["stage"], run["error"] = "error", "오류", f"실행하지 못했습니다: {type(e).__name__}"
    finally:
        promptlog.stop()
        cancel.clear(run["id"])   # 이 실행의 정지 신호를 정리(다음 실행에 남지 않게)
        _searches = budget.used(run["id"])       # 이 실행에서 쓴 검색 횟수(기록)
        _ctx_chars = budget.ctx_used(run["id"])  # 이 실행에서 모델에 전달한 누적 글자 수(기록)
        _ctx_pct, _ctx_line = budget.ctx_pct(run["id"]), budget.ctx_write_line(run["id"])   # 최종 문맥 사용률·작성 전환선(실행 후 확인용)
        budget.clear(run["id"])
        paused = run["status"] == "paused"
        if not paused:
            run["finished_at"] = _now()
            run["duration_s"] = round(time.time() - t0, 1)
        run["usage"] = dict(box)
        if _searches is not None:
            run["usage"]["searches"] = _searches   # 실행 전체 검색 도구 호출 수(단계별 조사 예산 대비)
        if _ctx_chars is not None:
            run["usage"]["context_chars"] = _ctx_chars   # 실행 전체 모델 전달 누적 글자 수(컨텍스트 예산 대비)
            run["usage"]["context_pct"] = _ctx_pct       # 권장선 대비 최종 사용률(%) — 75% 전후에서 멈췄는지 확인
            run["usage"]["context_write_line"] = _ctx_line   # 이 실행의 작성 전환선(글자)
        try:
            _save(run)
        finally:
            with _lock:
                if track_busy and not paused:   # 승인 대기 중에는 "이 캐릭터는 지금 바쁘다" 상태를 그대로 유지한다(다른 실행이 끼어들지 않게)
                    _active.pop(agent_id, None)
                _live.pop(run["id"], None)


def continue_truncated(agent_id: str, run_id: str) -> dict:
    """"이어서 쓰기": 잘린 곳부터 이어 받아 지금까지 쓴 글과 합친다. 도구는 다시 안 쓴다(비용 절감). 최대 MAX_CONTINUE 번까지."""
    run = get(agent_id, run_id)
    if run.get("status") != "paused":
        raise RunError("이 실행은 이어서 쓸 상태가 아닙니다(이미 끝났거나 승인 대기가 아닙니다).", 409)
    pause = run["pause"]
    if pause.get("continued", 0) >= MAX_CONTINUE:
        raise RunError(f"이어서 쓰기는 최대 {MAX_CONTINUE}번까지입니다. 중단한 뒤 처음부터 다시 실행하세요.", 409)
    run["status"], run["stage"] = "running", "이어서 쓰는 중"
    _save(run)
    threading.Thread(target=_continue_execute, args=(agent_id, run_id), name=f"continue-{run_id}", daemon=True).start()
    return run


def abort_truncated(agent_id: str, run_id: str) -> dict:
    """"중단": 승인 대기 중인 실행을 지금까지 쓴 글 그대로 실패 처리한다(처음부터 다시 돌리려면 별도로 새로 실행)."""
    run = get(agent_id, run_id)
    if run.get("status") != "paused":
        raise RunError("승인 대기 중인 실행이 아닙니다.", 409)
    pause = run["pause"]
    run["answer"] = pause["text_so_far"]
    run["status"], run["stage"], run["error"] = "error", "중단됨", "출력 한도 도달 후 사람이 중단했습니다(이어서 쓰지 않음)."
    run["finished_at"] = _now()
    _save(run)
    with _lock:
        _active.pop(agent_id, None)
        _live.pop(run_id, None)
    return run


def _continue_execute(agent_id: str, run_id: str) -> None:
    box = usage.start()
    t0 = time.time()
    run = get(agent_id, run_id)
    pause = run["pause"]
    try:
        agent = store.get(agent_id)
        task = run.get("task_snapshot") or find_task(agent, run["task_id"])
        from app.chat import tool_chat   # 순환 수입을 피하려고 쓸 때 불러온다(pipeline 도 같은 방식)
        _stage(run, "이어서 쓰는 중")
        turn = tool_chat.continue_claude(pause["session_state"], pause["text_so_far"])   # 이 호출 자체가 usage.add 로 box 에 더한다
        full_text = pause["text_so_far"] + turn.text
        continued = pause["continued"] + 1
        if turn.stop_reason == "max_tokens" and continued < MAX_CONTINUE:
            run["pause"] = {**pause, "text_so_far": full_text, "chars_so_far": len(full_text), "continued": continued,
                            "usage_last": turn.last_usage or {}, "paused_at": _now()}
            run["status"], run["stage"] = "paused", f"승인 대기: 또 출력 한도 도달 ({continued + 1}번째, {len(full_text):,}자 씀)"
        else:
            _stage(run, "형식 검사 중")
            chk = _verify_execution(task, full_text, agent_id, run)
            run["attempts"].append({"n": len(run["attempts"]) + 1, "ok": chk["ok"], "problems": chk["problems"],
                                    "mode": f"이어서 쓴 뒤 검사({continued}번째 이어쓰기)"})
            _check_and_status(task, run, full_text, chk, label="(이어서 씀)")
            run["pause"] = {**pause, "text_so_far": full_text, "chars_so_far": len(full_text), "continued": continued}
    except llm.ChatError as e:
        run["status"], run["stage"], run["error"] = "error", "오류", str(e)
    except Exception as e:
        log.exception("이어서 쓰기 오류 (%s)", agent_id)
        run["status"], run["stage"], run["error"] = "error", "오류", f"이어서 쓰지 못했습니다: {type(e).__name__}"
    finally:
        paused = run["status"] == "paused"
        if not paused:
            run["finished_at"] = _now()
            run["duration_s"] = round(time.time() - t0, 1)
        prev = run.get("usage") or {}
        add = dict(box)
        run["usage"] = {k: (prev.get(k) or 0) + (add.get(k) or 0) for k in set(prev) | set(add)}
        try:
            _save(run)
        finally:
            with _lock:
                if not paused:
                    _active.pop(agent_id, None)
                _live.pop(run_id, None)


_FENCE_ANY = re.compile(r"```[A-Za-z]*\r?\n(.*?)```", re.S)


def is_running(agent_id: str) -> str | None:
    with _lock:
        return _active.get(agent_id)


def recover() -> None:
    """서버가 꺼지면서 끝나지 못한 실행을 '중단됨'으로 표시한다(결과는 없다)."""
    for d in store.AGENTS_DIR.iterdir():
        rd = d / "runs"
        if not rd.is_dir():
            continue
        for p in rd.glob("r_*.json"):
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
                if r.get("status") == "running":
                    r.update(status="interrupted", stage="서버가 꺼져 중단됨", finished_at=_now())
                    store._write_json(p, r)
            except (OSError, ValueError):
                log.warning("실행 기록을 읽지 못했습니다: %s", p)
