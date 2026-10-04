"""업무 실행 때 모델에 보낸 프롬프트를 사람이 읽을 수 있는 로그 파일로 남긴다(2026-09-25).

목적: "그냥 기다리게만" 두지 않고, 무엇을 딥시크(또는 다른 모델)에 보냈는지·도구를 어떻게 부르며 진행 중인지를
실행 도중·이후에 파일로 확인할 수 있게 한다.

- 대상은 usage 처럼 contextvar 로 넘긴다. 업무 실행 스레드(_execute)가 시작할 때 열고, 같은 스레드에서
  도는 pipeline→tool_chat 이 그 파일에 스텝별로 쓴다. 대상이 없으면(대화 등) 아무것도 안 한다.
- 파일 위치: agents/<id>/runs/<run_id>.prompt.log. 실행마다 새로 쓴다.
"""

import contextvars
from datetime import datetime, timezone
from pathlib import Path

_sink: contextvars.ContextVar[dict | None] = contextvars.ContextVar("promptlog", default=None)

_MAX_TOOL_RESULT = 2000   # 도구 결과는 앞부분만(로그가 지나치게 커지지 않게)


def start(path: Path, *, agent_name: str, agent_id: str, task_name: str, provider: str, model: str) -> None:
    """이 실행의 프롬프트 로그를 연다. 실패해도(디스크 문제 등) 실행 자체는 막지 않는다."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "w", encoding="utf-8")
    except OSError:
        _sink.set(None)
        return
    _sink.set({"f": f, "step": 0})
    _line(f"=== 업무 실행 프롬프트 로그 ===")
    _line(f"캐릭터: {agent_name} ({agent_id})")
    _line(f"업무: {task_name}")
    _line(f"모델: {provider} / {model}")
    _line(f"시각: {datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}")
    _line("")


def _line(text: str) -> None:
    box = _sink.get()
    if not box:
        return
    try:
        box["f"].write(text + "\n")
        box["f"].flush()   # 실행 도중에도 열어서 볼 수 있게 즉시 기록
    except (OSError, ValueError):
        pass


def system_and_input(system: str, messages: list[dict]) -> None:
    """모델 호출 전, 시스템 프롬프트 전문과 업무 입력(user 메시지)을 남긴다."""
    _line(f"──────── 시스템 프롬프트 ({len(system):,}자) ────────")
    _line(system)
    _line("")
    for m in messages:
        if m.get("role") == "user":
            _line(f"──────── 업무 입력 (user, {len(m.get('content') or ''):,}자) ────────")
            _line(m.get("content") or "")
            _line("")


def step_calls(calls) -> None:
    """모델이 이번 스텝에 부른 도구(들)를 남긴다. calls 는 tool_chat.Call 목록."""
    box = _sink.get()
    if not box:
        return
    box["step"] += 1
    _line(f"──────── 스텝 {box['step']} · 모델이 도구 호출 ────────")
    for c in calls:
        _line(f"  → {c.name}({_short_json(c.args)})")


def step_results(results) -> None:
    """이번 스텝의 도구 실행 결과(앞부분)를 남긴다. results 는 (Call, text, ok) 목록."""
    for c, text, ok in results:
        head = (text or "")[:_MAX_TOOL_RESULT]
        more = "" if len(text or "") <= _MAX_TOOL_RESULT else f" …(총 {len(text):,}자, 앞부분만)"
        _line(f"  [{c.name} 결과 {'성공' if ok else '실패'}]{more}")
        _line("    " + head.replace("\n", "\n    "))
    _line("")


def final(text: str) -> None:
    _line(f"──────── 최종 답변 ({len(text or ''):,}자) ────────")
    _line(text or "(빈 답변)")
    _line("")


def note(text: str) -> None:
    _line(f"[알림] {text}")


def stop() -> None:
    box = _sink.get()
    if box:
        try:
            box["f"].close()
        except (OSError, ValueError):
            pass
    _sink.set(None)


def _short_json(obj) -> str:
    import json
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(obj)
    return s if len(s) <= 400 else s[:400] + "…"


def active() -> bool:
    return _sink.get() is not None
