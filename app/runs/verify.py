"""업무 결과 검증 파이프라인 (R1).

  1. 응답에서 JSON 추출  (전체가 JSON → ```json 코드펜스(마지막) → 마지막 JSON 객체)
  2. JSON 파싱
  3. 등록된 JSON Schema(Draft 2020-12)로 검증  ← `jsonschema` 패키지
  4. 간이 검사 설정(checks.py: coverage/refs/when …)
  5. 후처리 훅(파이썬 함수)  ← 별도 프로세스, 시간 제한

돌려주는 값: {"ok", "problems": [..], "kinds": [..], "json": 값|None}
  kinds: parse(JSON 을 못 읽음) / content(스키마·규칙 위반) / hook_error(훅 자체가 실패: 모델 잘못이 아니므로 다시 시키지 않는다)

후처리 훅은 **업로드한 코드를 서버 PC에서 실행**하는 기능이다. 그래서 (1) 올릴 때 사용자의 확인을 받고, (2) 서버와 분리된 프로세스에서 시간 제한을 걸어 실행하고,
(3) 등록할 때는 실행하지 않고 문법과 함수 이름만 확인한다(compile / ast).
"""

import ast
import hashlib
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from app.agents import store
from app.runs import checks, standard

log = logging.getLogger("agent_town.runs")

HOOK_TIMEOUT = int(os.getenv("RUN_HOOK_TIMEOUT") or 15)
MAX_HOOK_BYTES = 200 * 1024
MAX_SCHEMA_CHARS = 200_000
MAX_PROBLEMS = 20
RUNNER = Path(__file__).with_name("hook_runner.py")
MARK = "@@RESULT@@"


# ---------------------------------------------------------------- 스키마

def validate_schema(schema) -> dict:
    """저장 전에 JSON Schema(Draft 2020-12)가 올바른지 확인한다. 잘못이면 ValueError(사람이 읽을 문장)."""
    if not isinstance(schema, dict):
        raise ValueError("출력 스키마는 JSON 객체여야 합니다.")
    if len(json.dumps(schema, ensure_ascii=False)) > MAX_SCHEMA_CHARS:
        raise ValueError(f"출력 스키마가 너무 깁니다(최대 {MAX_SCHEMA_CHARS:,}자).")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as e:
        where = "/".join(str(p) for p in e.absolute_path) or "(root)"
        raise ValueError(f"JSON Schema 가 올바르지 않습니다 — {where}: {e.message[:200]}")
    return schema


def schema_problems(data, schema: dict) -> list[str]:
    validator = Draft202012Validator(schema)
    errs = sorted(validator.iter_errors(data), key=lambda e: [str(p) for p in e.absolute_path])
    out = []
    for e in errs[: MAX_PROBLEMS * 2]:
        where = "/".join(str(p) for p in e.absolute_path) or "(root)"
        out.append(f"{where}: {e.message[:300]}")
    return out


# ---------------------------------------------------------------- 후처리 훅 (파일 관리)

def _hook_dir(agent_id: str) -> Path:
    d = store.agent_dir(agent_id) / "hooks"
    d.mkdir(exist_ok=True)
    return d


def hook_meta(agent_id: str, task_id: str) -> dict | None:
    p = _hook_dir(agent_id) / f"{task_id}.json"
    if not (p.is_file() and (_hook_dir(agent_id) / f"{task_id}.py").is_file()):
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_hook(agent_id: str, task_id: str, filename: str, code: str, function: str) -> dict:
    """훅 파일을 저장한다. 실행하지 않고 문법(compile)과 함수 이름(ast)만 확인한다."""
    if len(code.encode("utf-8")) > MAX_HOOK_BYTES:
        raise ValueError(f"훅 파일이 너무 큽니다(최대 {MAX_HOOK_BYTES // 1024}KB).")
    if not function.isidentifier():
        raise ValueError("함수 이름이 올바르지 않습니다(영문·숫자·밑줄).")
    try:
        tree = ast.parse(code, filename=filename or "hook.py")
        compile(tree, filename or "hook.py", "exec")
    except SyntaxError as e:
        raise ValueError(f"파이썬 문법 오류: {e.msg} ({e.lineno}행)")
    names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if function not in names:
        raise ValueError(f"파일에 함수 {function}() 가 없습니다. 있는 함수: {', '.join(sorted(names)) or '(없음)'}")
    d = _hook_dir(agent_id)
    store._write_text(d / f"{task_id}.py", code)
    meta = {"filename": (filename or "hook.py")[:100], "function": function, "size": len(code.encode("utf-8")),
            "sha256": hashlib.sha256(code.encode("utf-8")).hexdigest()[:16], "updated_at": datetime.now(timezone.utc).isoformat(), "functions": sorted(names)}
    store._write_json(d / f"{task_id}.json", meta)
    return meta


def delete_hook(agent_id: str, task_id: str) -> bool:
    d = _hook_dir(agent_id)
    gone = False
    for ext in ("py", "json"):
        p = d / f"{task_id}.{ext}"
        if p.is_file():
            p.unlink()
            gone = True
    return gone


def prune_hooks(agent_id: str, keep_task_ids: set[str]) -> None:
    """지워진 업무의 훅 파일을 정리한다."""
    d = _hook_dir(agent_id)
    for p in d.glob("t_*.*"):
        if p.stem not in keep_task_ids:
            p.unlink()


def run_hook(agent_id: str, task_id: str, meta: dict, data) -> tuple[list[str], str | None]:
    """(오류 메시지 목록, 훅 자체의 오류). 훅이 멈추거나 죽으면 두 번째 값에 이유를 담는다.
    훅에는 검사할 데이터와 함께 표준 기준값(standard.hook_config)을 넘긴다 — 훅 모듈의 PLATFORM_CONFIG 로 들어간다."""
    path = _hook_dir(agent_id) / f"{task_id}.py"
    payload = {"__hook_payload__": 1, "data": data, "config": standard.hook_config()}
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-X", "utf8", str(RUNNER), str(path), meta["function"]],
            input=json.dumps(payload, ensure_ascii=False), capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=HOOK_TIMEOUT, cwd=str(path.parent), env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        return [], f"후처리 훅이 {HOOK_TIMEOUT}초 안에 끝나지 않아 중단했습니다."
    except OSError as e:
        return [], f"후처리 훅을 실행하지 못했습니다: {e}"
    line = next((ln for ln in reversed(proc.stdout.splitlines()) if ln.startswith(MARK)), None)
    if line is None:
        return [], f"후처리 훅이 결과를 내지 못했습니다(종료 코드 {proc.returncode}). {proc.stderr.strip()[-300:]}"
    out = json.loads(line[len(MARK):])
    if "error" in out:
        return [], f"후처리 훅 오류 — {out['error']}" + (f" ({' / '.join(out.get('trace', [])[-2:])})" if out.get("trace") else "")
    return [str(x) for x in out["errors"]], None


# ---------------------------------------------------------------- 전체 검증

def has_checks(task: dict, agent_id: str) -> bool:
    return bool(task.get("check") or task.get("output_schema") or (task.get("id") and hook_meta(agent_id, task["id"])))


def run(task: dict, text: str, agent_id: str, overrides: dict | None = None, sources: list[dict] | None = None) -> dict:
    """업무 결과 검증. overrides 로 저장하지 않은 스키마·간이 검사 설정을 시험해 볼 수 있다(검증 시험).
    sources: 이번 실행의 도구 기록 — 주면 공통 검증이 확인 여부를 실행 기록과 대조한다(표준 v1.1 §5).
    돌려주는 warnings 는 통과·실패에 영향이 없고 실행 기록에만 남는다."""
    ov = overrides or {}
    spec = ov["check"] if "check" in ov else task.get("check")
    schema = ov["output_schema"] if "output_schema" in ov else task.get("output_schema")
    schema = standard.apply_schema_thresholds(schema)   # "x-std-threshold" 표시 자리의 기준값을 설정에서 채운다(지시문과 같은 값)
    meta = hook_meta(agent_id, task["id"]) if task.get("id") else None
    member = standard.is_member(agent_id)
    if not (spec or schema or meta or member):
        return {"ok": True, "problems": [], "kinds": [], "json": None, "warnings": []}

    data, _, _, err = checks.locate_json(text)
    if err:
        return {"ok": False, "problems": [standard.with_code(err, "format") if member else err], "kinds": ["parse"], "json": None, "warnings": []}

    problems: list[str] = []
    kinds: set[str] = set()
    if schema:
        # 표준 적용 대상은 오류마다 분류 코드를 붙인다(재시도 정책이 코드로 갈린다). 스키마 위반은 format.
        problems += [standard.with_code(p, "format") if member else p for p in schema_problems(data, schema)]
    if spec:
        legacy = checks.run({**spec, "json": True}, text)
        problems += [p for p in legacy["problems"] if p not in problems]
    # 공통 검증(표준 적용 대상만): 날짜 일관성·확신도 상한·확인 여부 대조·금지 표현 — 직원 훅보다 먼저, 모든 직원에게 같은 코드
    common_errs, warnings = standard.check(data, text, sources, agent_id)
    problems += common_errs
    if problems:
        kinds.add("content")
    hook_error = None
    if meta:
        errs, hook_error = run_hook(agent_id, task["id"], meta, data)
        if errs:
            problems += [standard.classify(e) for e in errs] if member else errs   # 훅 오류에도 분류 코드(분류표는 standard.json 하나)
            kinds.add("content")
        if hook_error:
            problems.append(standard.with_code(hook_error, "legacy_unclassified") if member else hook_error)   # 훅 자체 고장: 모델 잘못이 아니므로 멈추고 기록
            kinds.add("hook_error")
    shown = problems[:MAX_PROBLEMS] + ([f"… 외 {len(problems) - MAX_PROBLEMS}건"] if len(problems) > MAX_PROBLEMS else [])
    out = {"ok": not problems, "problems": shown, "kinds": sorted(kinds), "json": data, "warnings": warnings[:MAX_PROBLEMS]}
    if member:   # 재시도 정책은 보이는 20건이 아니라 전체 문제의 코드로 가른다('… 외 N건' 요약 줄이 분류 안 됨으로 잡히지 않게)
        out["codes"] = sorted({standard.code_of(p) for p in problems})
    return out


# ---------------------------------------------------------------- 과거 결과로 재검사(2026-09-27)

RETEST_LIMIT = 20


def retest(agent_id: str, task: dict, limit: int = RETEST_LIMIT) -> dict:
    """검사기(후처리 훅·출력 스키마·간이 검사)를 바꾼 뒤, 이 업무의 최근 통과 결과를 지금 검사기로 다시 검사한다(모델 호출 없음).
    예전엔 통과했는데 지금은 떨어지는 결과가 있으면 검사기가 멀쩡한 결과를 막는다는 뜻이다.
    경고 통과 허용 목록(app/runs/soft.py)에 드는 문제만 남으면 통과로 센다. 실행 기록이 필요한 대조(도구 원본·인용 원문)는 하지 않는다."""
    from app.runs import service, soft   # service 가 이 모듈을 import 하므로 쓸 때 불러온다
    passed = [r for r in service.list_runs(agent_id, task["id"], limit=200) if r.get("status") in ("ok", "warning")][:limit]
    newly, still = [], 0
    for s in passed:
        try:
            r = service.get(agent_id, s["id"])
        except Exception:
            continue
        res = run(task, r.get("answer") or "", agent_id)
        if res["ok"] or ("hook_error" not in res["kinds"] and soft.all_soft(res["problems"])):
            still += 1
        else:
            newly.append({"run_id": r["id"], "started_at": r.get("started_at"), "problems": res["problems"][:5]})
    return {"checked": len(passed), "still_pass": still, "newly_failed": newly,
            "note": "형식·후처리 훅만 다시 검사했습니다(도구 원본·인용 원문 대조는 실행 기록이 있어야 해서 제외)."}
