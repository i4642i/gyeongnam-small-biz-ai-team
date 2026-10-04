"""도구 호출 반복(3사 공통): 모델이 도구를 부르면 서버가 실행해서 결과를 돌려주고, 모델이 최종 답을 낼 때까지 반복한다.

구조
  run()               공통 반복: 한 번 부르고 → 도구 요청이 있으면 실행 → 결과를 붙여 다시 부름 (최대 MAX_STEPS 번, 그다음은 도구 없이 답을 받는다)
  _Claude / _OpenAI / _Gemini   공급사마다 다른 요청·응답 모양을 이 안에서만 다룬다.
      step(allow_tools)  → Turn(text, calls)         모델을 한 번 부른다
      add_results(...)                               도구 결과를 그 공급사 형식으로 대화에 덧붙인다
Claude 는 Anthropic SDK, OpenAI 는 openai SDK, Gemini 는 google-genai SDK 를 각자 쓴다(서로의 형식을 섞지 않는다).
도구 자체(정의·검증·한도·실행)는 app/tools 가 맡는다.
"""

import json
import logging
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

import anthropic

from app.chat import providers, usage
from app.chat.providers import ChatError, HardTruncated, Truncated
from app.tools import registry, runner

log = logging.getLogger("agent_town.tool_chat")

MAX_CALLS_PER_TURN = 4   # 모델이 한 번에 여러 도구를 부를 때 실행하는 수(나머지는 오류로 돌려준다)
MAX_CALLS_PER_TURN_TASK = int(os.getenv("TOOL_MAX_PARALLEL_TASK") or 16)   # 업무 실행은 계산 도구를 한꺼번에 부르는 일이 많아 더 넉넉히(첫 지정학 실행에서 8개 한도에 걸림)


@dataclass
class Call:
    id: str
    name: str
    args: dict


@dataclass
class Turn:
    text: str
    calls: list[Call] = field(default_factory=list)
    stop_reason: str | None = None   # Claude 만 채운다(max_tokens 감지용)
    last_usage: dict | None = None   # 이 턴 호출의 토큰 사용량(Claude 만, 이어서 쓰기 화면에 보여줄 값)


# ---------------------------------------------------------------- 공통 반복

def run(provider: str, model: str, system: str, messages: list[dict], tool_defs: list, agent_id: str, mode: str = "chat",
        inputs: dict | None = None, max_tokens: int | None = None, cancel_run_id: str | None = None) -> tuple[str, list[dict]]:
    """(최종 답변, 도구 사용 기록). 도구 사용 기록은 화면과 대화 기록에 남길 요약이다.
    max_tokens: 이 업무 전용 출력 한도(없으면 공급사 기본값 — providers.CLAUDE_MAX_TOKENS 등). 지금은 Claude 만 실제로 쓴다."""
    session = _SESSIONS[provider](model, system, messages, [registry.spec(t) for t in tool_defs], max_tokens=max_tokens)
    if mode == "task" and hasattr(session, "cache"):
        session.cache = True   # 업무 실행은 한 번에 모델을 여러 번 부르므로 캐싱이 이득이다(대화는 한 번만 부르는 경우가 많아 켜지 않는다)
    from app.chat import promptlog
    promptlog.system_and_input(system, messages)   # 실행 기록에서 볼 수 있게 전송 프롬프트를 로그로 남긴다(2026-09-25)
    by_name = {t.id: t for t in tool_defs}
    ctx = runner.Context(agent_id, mode=mode, inputs=inputs or {})
    trace: list[dict] = []
    max_steps = registry.steps_limit(mode)
    per_turn = MAX_CALLS_PER_TURN_TASK if mode == "task" else MAX_CALLS_PER_TURN
    search_cap = registry.MAX_SEARCH_PER_TURN_TASK if mode == "task" else per_turn   # 라운드당 조회 도구 상한(계산 도구·작은 조회 제외)

    from app.runs import budget, cancel
    # 단계별 조사: 실행 전체 검색 예산이 소진되거나 문맥이 전환선에 닿으면 조회 도구를 껐다(오류 반복이 아니라) 작성으로 넘어간다.
    # 예산 키는 run_id(= cancel_run_id). service 가 budget.set_budget 으로 미리 설정한다(대화 등 미설정 run 은 제한 없음).
    search_run_id = cancel_run_id
    has_search = any(registry.is_search_tool(t) for t in tool_defs)
    keep_specs = [registry.spec(t) for t in tool_defs if not registry.is_retrieval_tool(t)]   # 조회 도구를 끈 뒤에도 남기는 계산·조립 도구
    has_retrieval = len(keep_specs) < len(tool_defs)
    search_disabled = False
    # 문맥은 대화 전체로 센다: 시스템 프롬프트 + 도구 정의 + 업무 입력(팀장·운용은 보고서·후보 목록이 대부분) — 도구 결과는 라운드마다 더한다.
    budget.add_base(search_run_id, len(system or "") + sum(len(json.dumps(registry.spec(t), ensure_ascii=False)) for t in tool_defs)
                    + sum(_content_chars(m) for m in messages))

    for step in range(max_steps + 1):
        if cancel_run_id and cancel.is_set(cancel_run_id):   # '작업 정지': 다음 모델 왕복을 하지 않는다
            raise cancel.Cancelled("사용자가 실행을 정지시켰습니다.")
        if has_retrieval and not search_disabled and (budget.hard_reached(search_run_id) or budget.ctx_hard_reached(search_run_id)
                                                      or budget.ctx_write_guard(search_run_id)):
            session.restrict_tools(keep_specs)   # 검색 하드 한도, 또는 문맥 작성 전환선(75%·작성 여유 확보) 도달 → 조회 도구 제거(계산 도구는 유지)
            search_disabled = True
            promptlog.note(f"안전선 도달(검색 횟수 또는 문맥 사용률 {budget.ctx_pct(search_run_id)}%) — 조회 도구를 끄고 작성으로 전환합니다.")
        last = step >= max_steps            # 반복 한도: 이번에는 도구 없이 답을 받는다
        turn = session.step(allow_tools=not last)
        if not turn.calls:
            if turn.stop_reason == "max_tokens" and hasattr(session, "session_state"):
                # 도구 호출 없이(=마지막 답을 쓰던 중) 출력 한도에 걸렸다. 처음부터 다시(도구 호출까지 전부) 시키면
                # 비용이 크므로, 사람이 "이어서 쓰기/중단"을 고를 수 있게 지금까지 쓴 글과 세션 상태를 올린다.
                raise Truncated(turn.text, trace, session.session_state(), getattr(session, "last_usage", None))
            if turn.stop_reason in ("length", "max_tokens"):
                # 이어쓰기 세션이 없는 공급사(deepseek/openai)의 출력 잘림. 잘린 글을 그대로 최종답으로 넘기면 형식 검증이
                # 'JSON 없음'으로 오인해 도구까지 다시 쓰는 재시도를 돌린다(비용·실패). 초안·기록·적용 한도를 담아 올려,
                # _execute 가 '검색 없이 최대 1회 재작성' 복구를 하거나 미완료로 종료하게 한다.
                promptlog.final(_strip_dsml(turn.text))
                raise HardTruncated(_strip_dsml(turn.text), trace, getattr(session, "last_applied", None))
            text = _strip_dsml(turn.text)
            if not text:
                raise ChatError("빈 답변이 왔습니다. 다시 시도하세요.")
            promptlog.final(text)
            return text, trace, _history(session)
        if last:                                     # 도구를 못 쓰게 했는데도 요청한 경우: 가진 글이 있으면 그것을 답으로
            text = _strip_dsml(turn.text)
            if text:
                promptlog.final(text)
                return text, trace, _history(session)
            raise ChatError("도구 사용 한도에 도달했지만 답변이 만들어지지 않았습니다. 다시 시도하세요.")

        promptlog.step_calls(turn.calls)
        # 모델이 이번에 쓴 글(도구 호출 인자 포함)도 다음 호출부터 대화에 실린다 → 문맥에 더한다.
        budget.add_base(search_run_id, len(turn.text or "") + sum(len(c.name) + len(json.dumps(c.args, ensure_ascii=False)) for c in turn.calls))
        results = []
        round_search = 0     # 이번 라운드 검색 도구 실행 수(검색 예산 안내용)
        round_capped = 0     # 이번 라운드 '결과가 큰 조회 도구' 실행 수(라운드당 상한)
        for i, call in enumerate(turn.calls):
            tool = by_name.get(call.name)
            if i >= per_turn:
                res = _refuse(tool, call, f"한 번에 부를 수 있는 도구는 {per_turn}개까지입니다.")
            elif tool is None:
                res = _refuse(None, call, f"'{call.name}'라는 도구는 없습니다. 쓸 수 있는 도구: {', '.join(by_name)}")
            elif registry.is_retrieval_tool(tool) and search_disabled:
                # 목록에서 뺀 뒤에도 이미 알던 조회 도구를 부르는 경우 — 실행하지 않는다(문맥을 더 채우지 않게).
                res = _refuse(tool, call, "문맥·검색 안전선에 도달해 조회 도구가 꺼졌습니다. 추가 조사는 하지 말고 지금 가진 자료로 보고서와 JSON 을 작성하세요.")
            elif mode == "task" and registry.is_round_capped(tool) and round_capped >= search_cap:
                res = _refuse(tool, call, f"한 라운드에 조회 도구는 {search_cap}개까지만 실행합니다(결과가 한꺼번에 쌓이는 것 방지). 꼭 필요한 것만 다음 라운드에 다시 부르세요.")
            elif registry.is_search_tool(tool) and not budget.consume(search_run_id):
                # 하드 한도 도달: 이 검색은 거부하고 작성으로 넘긴다(한 라운드 안에서 남은 예산을 넘겨 부른 경우 등).
                res = _refuse(tool, call, "검색 하드 한도에 도달했습니다. 더 검색하지 말고 확보한 자료로 보고서와 JSON 을 완성하세요(확인 못 한 항목은 미확인으로 표시).")
            else:
                if registry.is_search_tool(tool):
                    round_search += 1
                if registry.is_round_capped(tool):
                    round_capped += 1
                res = runner.execute(tool, call.args, ctx)
            trace.append(res.entry)
            results.append((call, res.text, res.ok))
        # 모델에게 전달한 도구 결과 글자 수를 컨텍스트 예산에 누적(§6). 현황 안내는 아래에서 붙인다.
        budget.add_delivered(search_run_id, sum(len(r[1] or "") for r in results))
        # 진행 현황 안내: 이번 라운드 결과 끝에 라운드·문맥·검색 현황을 붙여 다음 라운드에 모델이 보게 한다 — 검색 도구가 없는
        # 팀장·운용 등도 라운드·문맥은 보여야 한다(제약을 거는 것과 그 제약을 모델에게 보이게 하는 것은 다른 일).
        if results and search_run_id and (budget.ctx_pct(search_run_id) is not None or budget.remaining(search_run_id) is not None):
            note = _budget_status(tool_defs, ctx, search_run_id, round_search, max_steps - step, search_disabled, has_search, search_cap)
            budget.add_base(search_run_id, len(note))
            c, txt, ok = results[-1]
            results[-1] = (c, (txt or "") + note, ok)
        promptlog.step_results(results)
        session.add_results(results)
    raise ChatError("도구 사용 반복이 끝나지 않았습니다. 다시 시도하세요.")   # 도달하지 않는다(위에서 last 처리)


def _content_chars(m) -> int:
    """대화 메시지 하나의 글자 수(문맥 집계용). 내용이 글이 아니면(블록 목록 등) JSON 으로 센다."""
    c = m.get("content") if isinstance(m, dict) else m
    return len(c) if isinstance(c, str) else len(json.dumps(c, ensure_ascii=False, default=str))


# 진행 단계: 0 조사 계속 / 1 마무리·정리 / 2 이번 라운드까지만 도구 / 3 작성(도구 불가). 값(60·75%, 라운드 5·2)은 budget.py 에서 조정.
_STAGE = {0: "조사 계속 가능", 1: "조사 마무리·정리 단계", 2: "이번 라운드까지만 도구 사용", 3: "지금 작성·도구 사용 불가"}
_MARK = {0: "", 1: "△", 2: "⚠", 3: "⛔"}


def _budget_status(tool_defs: list, ctx, run_id: str, round_search: int, rounds_left: int, disabled: bool,
                   has_search: bool = True, cap: int = 4) -> str:
    """진행 현황 한 줄(소프트 안내): 남은 라운드·문맥 사용률·검색 잔여를 가장 급한 것부터 보이고, 가장 급한 단계의 행동을 알려 준다.
    도구 결과 끝에 붙여 다음 라운드에 모델이 본다. '검색 44회 남음 = 여유'로 오판하고 라운드·문맥이 먼저 바닥난 실패(지정학) 후 도입."""
    from app.runs import budget
    items = []   # (단계, 기본 순서, 글)
    # 남은 라운드 — 마지막 '작성 전용' 라운드까지 포함한 수. 1이면 다음 호출은 도구 없이 작성만 된다.
    r_lv = 3 if rounds_left <= 1 else 2 if rounds_left <= budget.ROUNDS_LAST_TOOL else 1 if rounds_left <= budget.ROUNDS_WRAP else 0
    items.append((r_lv, 0, f"남은 라운드 {rounds_left}"))
    pct = budget.ctx_pct(run_id)          # 대화 전체(시스템·입력·출력·도구 결과) 사용률(%), 미설정이면 None
    if pct is not None:
        ctx_off = budget.ctx_hard_reached(run_id) or budget.ctx_write_guard(run_id)   # 전환선(75%·작성 여유) 도달 → 다음 라운드부터 조회 도구 꺼짐
        c_lv = 3 if ctx_off else 2 if budget.ctx_next_round_crosses(run_id) else 1 if pct >= budget.CTX_WRAP_PCT else 0
        items.append((c_lv, 1, f"문맥 사용률 {pct}%"))
    rem = budget.remaining(run_id)        # 소프트 잔여(0에서 멈춤)
    if has_search and rem is not None:
        s_lv = 3 if budget.hard_reached(run_id) else 2 if rem <= 3 else 1 if rem <= 10 else 0
        per = [f"{t.id} {registry.calls_limit(t, ctx.mode) - ctx.counts.get(t.id, 0)}" for t in tool_defs if registry.is_search_tool(t)]
        items.append((s_lv, 2, f"검색 잔여 {rem}(검색 누적 {budget.used(run_id)}/{budget.soft(run_id)}, 이번 라운드 {round_search}회; "
                               f"도구별 잔여 {', '.join(per)})"))
    items.sort(key=lambda x: (-x[0], x[1]))     # 가장 급한 것을 앞에
    level = max(items[0][0], 3 if disabled else 0)
    shown = " · ".join(f"{_MARK[lv]}{txt}({_STAGE[lv]})" for lv, _, txt in items)
    if level == 3 and rounds_left <= 1:
        act = "지금 자료로 보고서와 필수 JSON 을 작성하세요. 도구 사용 불가 — 다음 호출은 도구 없이 작성만 됩니다(확인 못 한 항목은 미확인으로 표시)."
    elif level == 3:
        act = ("조회 도구가 꺼졌습니다. 추가 조사는 하지 말고 지금 자료로 보고서와 필수 JSON 을 작성하세요"
               "(계산 도구는 남은 값 계산에만 쓰고, 확인 못 한 항목은 미확인으로 표시).")
    elif level == 2:
        act = (f"이번 라운드까지만 도구를 쓸 수 있습니다 — 꼭 필요한 조회만 이번에 한꺼번에(조회 도구 라운드당 {cap}개까지) 하고, "
               "다음 라운드부터 작성하세요.")
    elif level == 1:
        act = "조사를 마무리하고 정리 단계로 넘어가세요 — 새 사건·새 자료 발굴을 멈추고 꼭 필요한 확인만 하세요."
    else:
        act = "조사 계속 가능."
    return f"\n\n〔자료 예산·진행 현황〕 {shown} → {act}"


def _strip_dsml(text: str) -> str:
    """deepseek 가 최종 답에 도구호출 마크업(<｜｜DSML｜｜ ... calls> …)을 텍스트로 흘리는 경우 제거(#2 누출 방지).
    바 문자에 의존하지 않게 'DSML' 이 든 태그·블록·줄을 통째로 걷어낸다."""
    if not text:
        return ""
    if "DSML" in text:
        text = re.sub(r"<[^<>]*DSML[^<>]*calls>.*?</[^<>]*DSML[^<>]*calls>", "", text, flags=re.S)
        text = re.sub(r"<[^<>]*DSML[^<>]*>", "", text)
        text = "\n".join(ln for ln in text.splitlines() if "DSML" not in ln)
    return text.strip()


def _history(session):
    """재시도가 이어받을 대화 이력(system 제외, 도구 호출·결과 포함). 지원 안 하는 공급사는 None(#1: 수집 관측 유실 방지)."""
    fn = getattr(session, "history", None)
    return fn() if callable(fn) else None


def _refuse(tool, call: Call, message: str) -> runner.Result:
    name = call.name
    return runner.Result(False, f'<도구결과 도구="{name}">\n(도구 오류)\n{message}\n</도구결과>',
                         {"tool": name, "label": (tool.label if tool else name) or name, "args": call.args if isinstance(call.args, dict) else {},
                          "ok": False, "summary": message[:200], "items": [], "text": message[:600], "ms": 0})


# ---------------------------------------------------------------- Claude (Anthropic SDK)

@contextmanager
def _claude_errors():
    try:
        yield
    except anthropic.AuthenticationError:
        raise ChatError("Claude 인증에 실패했습니다. .env의 ANTHROPIC_API_KEY를 확인하세요.")
    except anthropic.NotFoundError:
        raise ChatError("Claude 모델을 찾을 수 없습니다.")
    except anthropic.RateLimitError:
        raise ChatError("Claude 요청이 너무 많습니다. 잠시 후 다시 시도하세요.")
    except anthropic.APIConnectionError:
        raise ChatError("Claude 서버에 연결하지 못했습니다. 인터넷 연결을 확인하세요.")
    except anthropic.APIStatusError as e:
        raise ChatError(f"Claude 호출 실패 ({e.status_code}): {e.message}")


RETRY_ATTEMPTS = int(os.getenv("CLAUDE_RETRY_ATTEMPTS") or 3)   # 원 요청 포함 — 기본은 최초 1번 + 재시도 2번
_RETRY_STATUS = {500, 502, 503, 529}   # 서버 쪽 일시적 오류(과부하 등) — 인증 실패 같은 확정적 오류는 재시도하지 않는다


def _call_claude(fn):
    """연결 실패·서버 과부하처럼 일시적인 오류는 최대 RETRY_ATTEMPTS 번(원 요청 포함)까지 짧게 기다렸다 자동으로 다시 부른다.
    인증 실패 등 다시 불러도 똑같이 실패할 오류는 바로 올린다(재시도하지 않는다)."""
    last_exc: Exception | None = None
    for i in range(RETRY_ATTEMPTS):
        try:
            return fn()
        except anthropic.APIConnectionError as e:
            last_exc = e
        except anthropic.APIStatusError as e:
            if e.status_code not in _RETRY_STATUS:
                raise
            last_exc = e
        if i < RETRY_ATTEMPTS - 1:
            time.sleep(2.0 * (i + 1))
    raise last_exc


class _Claude:
    def __init__(self, model: str, system: str, messages: list[dict], specs: list[dict], max_tokens: int | None = None):
        self.model, self.system, self.messages = model, system, list(messages)
        self.tools = [{"name": s["name"], "description": s["description"], "input_schema": s["schema"]} for s in specs]
        self.cache = False   # True 면 요청마다 자동 프롬프트 캐싱(top-level cache_control)을 켠다
        self.last_usage: dict | None = None   # 마지막 호출의 토큰 사용량(잘렸을 때 화면에 보여줄 값)
        self.max_tokens = max_tokens or providers.CLAUDE_MAX_TOKENS   # 이 업무 전용 출력 한도(없으면 공급사 기본값)

    def restrict_tools(self, specs: list[dict]) -> None:   # 검색 예산 소진 시 검색 도구를 뺀 목록으로 교체
        self.tools = [{"name": s["name"], "description": s["description"], "input_schema": s["schema"]} for s in specs]

    def step(self, allow_tools: bool = True) -> Turn:
        kwargs = dict(model=self.model, max_tokens=self.max_tokens, system=self.system, messages=self.messages, tools=self.tools)
        if not allow_tools:
            kwargs["tool_choice"] = {"type": "none"}   # 대화에 tool_use 블록이 있으면 tools 를 계속 보내야 하므로, 정의는 두고 쓰지 못하게 한다
        if "haiku" not in self.model:
            kwargs["output_config"] = {"effort": "medium"}
        if self.model in ("claude-opus-5", "claude-fable-5-1"):
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        if self.cache:
            kwargs["cache_control"] = {"type": "ephemeral"}   # 마지막 캐시 가능 블록에 자동으로 붙는다(대화가 길어지면 뒤로 옮겨 간다)
        with _claude_errors():
            resp = _call_claude(lambda: providers.claude_client().beta.messages.create(**kwargs))
            usage.from_claude(resp)
        u = getattr(resp, "usage", None)
        if u is not None:
            self.last_usage = {"input_tokens": getattr(u, "input_tokens", 0) or 0, "output_tokens": getattr(u, "output_tokens", 0) or 0,
                                "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0, "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0}
        if resp.stop_reason == "refusal":
            raise ChatError("이 요청에는 답할 수 없습니다.")
        calls = [Call(b.id, b.name, dict(b.input or {})) for b in resp.content if b.type == "tool_use"]
        text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
        if calls:   # 생각 블록 등을 그대로 돌려줘야 한다 — JSON 으로 그대로 저장할 수 있게 dict 로 바꿔 둔다(잘렸을 때 세션을 파일에 저장해야 해서)
            self.messages.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})
        return Turn(text, calls, stop_reason=resp.stop_reason)

    def session_state(self) -> dict:
        """지금까지의 대화 상태(전부 JSON 으로 그대로 저장 가능) — 출력이 잘렸을 때 이어 쓰는 데 쓴다."""
        return {"provider": "anthropic", "model": self.model, "system": self.system, "messages": self.messages,
                "tools": self.tools, "max_tokens": self.max_tokens}

    def add_results(self, results: list[tuple[Call, str, bool]]) -> None:
        # 한 번에 부른 도구의 결과는 반드시 한 메시지에 모두 담는다
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": c.id, "content": text, **({} if ok else {"is_error": True})} for c, text, ok in results]})


def continue_claude(state: dict, text_so_far: str) -> Turn:
    """"이어서 쓰기": session_state() 로 저장해 둔 대화에 지금까지 쓴 글을 마지막 assistant 턴으로 붙여 이어받는다.
    도구는 다시 쓰지 않는다(끊긴 자리는 이미 도구 호출이 끝나고 마지막 글을 쓰던 중이었다 — 처음부터 다시 돌면
    도구 호출까지 전부 반복돼 비용이 크므로, 순수하게 텍스트만 이어 쓰게 한다)."""
    messages = state["messages"] + [{"role": "assistant", "content": text_so_far}]
    kwargs = dict(model=state["model"], max_tokens=state.get("max_tokens") or providers.CLAUDE_MAX_TOKENS, system=state["system"], messages=messages,
                  tools=state.get("tools") or [], tool_choice={"type": "none"})
    if "haiku" not in state["model"]:
        kwargs["output_config"] = {"effort": "medium"}
    if state["model"] in ("claude-opus-5", "claude-fable-5-1"):
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    with _claude_errors():
        resp = _call_claude(lambda: providers.claude_client().beta.messages.create(**kwargs))
        usage.from_claude(resp)
    if resp.stop_reason == "refusal":
        raise ChatError("이 요청에는 답할 수 없습니다.")
    text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
    u = getattr(resp, "usage", None)
    last_usage = None
    if u is not None:
        last_usage = {"input_tokens": getattr(u, "input_tokens", 0) or 0, "output_tokens": getattr(u, "output_tokens", 0) or 0,
                      "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0, "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0}
    return Turn(text, [], stop_reason=resp.stop_reason, last_usage=last_usage)


# ---------------------------------------------------------------- OpenAI (openai SDK)

class _OpenAI:
    def __init__(self, model: str, system: str, messages: list[dict], specs: list[dict], max_tokens: int | None = None):
        self.model = model
        self.max_tokens = max_tokens   # 업무별 출력 한도(tasks.json 의 max_output_tokens). 없으면 공급사 기본값.
        self.messages = [{"role": "system", "content": system}, *messages]
        self.tools = [{"type": "function", "function": {"name": s["name"], "description": s["description"], "parameters": s["schema"]}} for s in specs]

    def restrict_tools(self, specs: list[dict]) -> None:   # 검색 예산 소진 시 검색 도구를 뺀 목록으로 교체
        self.tools = [{"type": "function", "function": {"name": s["name"], "description": s["description"], "parameters": s["schema"]}} for s in specs]

    def history(self) -> list[dict]:   # 재시도가 이어받을 대화(system 제외, 도구 호출·결과 포함)
        return [dict(m) for m in self.messages[1:]]

    def step(self, allow_tools: bool = True) -> Turn:
        import openai

        client = openai.OpenAI(api_key=providers._key("openai"))

        def call(limit: int):
            r = client.chat.completions.create(model=self.model, max_completion_tokens=limit, messages=self.messages,
                                               tools=self.tools, tool_choice="auto" if allow_tools else "none")
            usage.from_openai(r)
            return r

        requested = self.max_tokens or providers.OPENAI_MAX_TOKENS
        applied = requested
        try:
            try:
                resp = call(applied)
            except openai.BadRequestError as e:
                if not providers._output_cap_exceeded(e.message):   # 출력 상한 초과가 '명확'할 때만 폴백(입력/컨텍스트 초과는 제외)
                    raise
                try:
                    applied = providers.OPENAI_MAX_TOKENS
                    resp = call(applied)
                except openai.BadRequestError as e2:
                    if not providers._output_cap_exceeded(e2.message):
                        raise
                    applied = providers.FALLBACK_MAX_TOKENS
                    resp = call(applied)
        except openai.AuthenticationError:
            raise ChatError("OpenAI 인증에 실패했습니다. .env의 OPENAI_API_KEY를 확인하세요.")
        except openai.NotFoundError:
            raise ChatError(f"OpenAI 모델을 찾을 수 없습니다: {self.model}")
        except openai.RateLimitError:
            raise ChatError("OpenAI 요청이 너무 많거나 사용 한도를 넘었습니다.")
        except openai.APIConnectionError:
            raise ChatError("OpenAI 서버에 연결하지 못했습니다. 인터넷 연결을 확인하세요.")
        except openai.APIStatusError as e:
            raise ChatError(f"OpenAI 호출 실패 ({e.status_code}): {e.message}")

        choice = resp.choices[0]
        fr = choice.finish_reason
        self.last_applied, self.last_finish_reason = applied, fr
        reduced = applied == providers.FALLBACK_MAX_TOKENS and requested > applied
        usage.note(applied_max_tokens=applied, finish_reason=fr)
        if reduced:
            usage.note(output_limit_reduced=True)
        if fr == "content_filter":
            raise ChatError("OpenAI가 이 요청의 답변을 차단했습니다.")
        msg = choice.message
        raw_calls = [tc for tc in (msg.tool_calls or []) if getattr(tc, "type", "function") == "function"]
        calls = [Call(tc.id, tc.function.name, _json_args(tc.function.arguments)) for tc in raw_calls]
        if calls:
            self.messages.append({"role": "assistant", "content": msg.content, "tool_calls": [
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}} for tc in raw_calls]})
        return Turn((msg.content or "").strip(), calls, stop_reason=("length" if choice.finish_reason == "length" else None))

    def add_results(self, results: list[tuple[Call, str, bool]]) -> None:
        for c, text, _ok in results:
            self.messages.append({"role": "tool", "tool_call_id": c.id, "content": text})


def _json_args(raw) -> dict:
    try:
        v = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {"_invalid_json": str(raw)[:200]}     # 검증에서 '모르는 인자'로 걸러져 모델이 고쳐 부른다
    return v if isinstance(v, dict) else {"_invalid_json": str(raw)[:200]}


# ---------------------------------------------------------------- DeepSeek (openai SDK, base_url 만 다름)
# _OpenAI 와 거의 같다 — 딥시크가 OpenAI 호환 형식을 그대로 쓰므로(2026-09-25, 설계안:
# docs/work_orders/설계안_딥시크_활용_2026-09-25.md). 도구 호출 메시지 모양도 동일해서 새로 만들 게 거의 없다.

class _DeepSeek:
    def __init__(self, model: str, system: str, messages: list[dict], specs: list[dict], max_tokens: int | None = None):
        self.model = model
        self.max_tokens = max_tokens   # 업무별 출력 한도(tasks.json 의 max_output_tokens). 없으면 공급사 기본값을 쓴다.
        self.messages = [{"role": "system", "content": system}, *messages]
        self.tools = [{"type": "function", "function": {"name": s["name"], "description": s["description"], "parameters": s["schema"]}} for s in specs]

    def restrict_tools(self, specs: list[dict]) -> None:   # 검색 예산 소진 시 검색 도구를 뺀 목록으로 교체
        self.tools = [{"type": "function", "function": {"name": s["name"], "description": s["description"], "parameters": s["schema"]}} for s in specs]

    def history(self) -> list[dict]:   # 재시도가 이어받을 대화(system 제외, 도구 호출·결과 포함)
        return [dict(m) for m in self.messages[1:]]

    def step(self, allow_tools: bool = True) -> Turn:
        import openai

        client = providers._deepseek_client()   # 타임아웃 + 자동 재시도 포함(2026-09-25)

        def call(limit: int):
            r = client.chat.completions.create(model=self.model, max_completion_tokens=limit, messages=self.messages,
                                               tools=self.tools, tool_choice="auto" if allow_tools else "none")
            usage.from_openai(r)
            return r

        requested = self.max_tokens or providers.OPENAI_MAX_TOKENS
        applied = requested
        try:
            try:
                resp = call(applied)
            except openai.BadRequestError as e:
                if not providers._output_cap_exceeded(e.message):   # 출력 상한 초과가 '명확'할 때만 폴백(입력/컨텍스트 초과는 제외)
                    raise
                # 요청한 출력 한도가 모델 상한 초과 — 알려진 안전값(16000)으로, 그것도 안 되면 최소값(4096)으로 낮춘다(예전보다 나빠지지 않게).
                try:
                    applied = providers.OPENAI_MAX_TOKENS
                    resp = call(applied)
                except openai.BadRequestError as e2:
                    if not providers._output_cap_exceeded(e2.message):
                        raise
                    applied = providers.FALLBACK_MAX_TOKENS
                    resp = call(applied)
        except openai.AuthenticationError:
            raise ChatError("DeepSeek 인증에 실패했습니다. .env의 DEEPSEEK_API_KEY를 확인하세요.")
        except openai.NotFoundError:
            raise ChatError(f"DeepSeek 모델을 찾을 수 없습니다: {self.model}")
        except openai.RateLimitError:
            raise ChatError("DeepSeek 요청이 너무 많거나 사용 한도를 넘었습니다.")
        except openai.APIConnectionError:
            raise ChatError("DeepSeek 서버에 연결하지 못했습니다. 인터넷 연결을 확인하세요.")
        except openai.APIStatusError as e:
            raise ChatError(f"DeepSeek 호출 실패 ({e.status_code}): {e.message}")

        choice = resp.choices[0]
        fr = choice.finish_reason
        # 최종 적용된 출력 한도와 finish_reason 을 기록한다(run["usage"] 로 저장·표시). 4096 까지 내려갔으면 '작성 여유 크게 줄었음' 표시.
        self.last_applied, self.last_finish_reason = applied, fr
        reduced = applied == providers.FALLBACK_MAX_TOKENS and requested > applied
        usage.note(applied_max_tokens=applied, finish_reason=fr)
        if reduced:
            usage.note(output_limit_reduced=True)
        if fr == "content_filter":
            raise ChatError("DeepSeek가 이 요청의 답변을 차단했습니다.")
        msg = choice.message
        raw_calls = [tc for tc in (msg.tool_calls or []) if getattr(tc, "type", "function") == "function"]
        calls = [Call(tc.id, tc.function.name, _json_args(tc.function.arguments)) for tc in raw_calls]
        if calls:
            self.messages.append({"role": "assistant", "content": msg.content, "tool_calls": [
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}} for tc in raw_calls]})
        # finish_reason="length": 출력이 최대 길이에 걸려 잘렸다. stop_reason 으로 올려 run() 이 '잘림'을 형식 오류와 구분하게 한다.
        return Turn((msg.content or "").strip(), calls, stop_reason=("length" if fr == "length" else None))

    def add_results(self, results: list[tuple[Call, str, bool]]) -> None:
        for c, text, _ok in results:
            self.messages.append({"role": "tool", "tool_call_id": c.id, "content": text})


# ---------------------------------------------------------------- Gemini (google-genai SDK)

class _Gemini:
    def __init__(self, model: str, system: str, messages: list[dict], specs: list[dict], max_tokens: int | None = None):
        # max_tokens: 지금은 Claude 전용 업무별 출력 한도 — Gemini 는 아직 자기 기본값(providers.GEMINI_MAX_TOKENS)을 쓴다.
        from google.genai import types

        self.model, self.system = model, system
        self.contents = [types.Content(role="user" if m["role"] == "user" else "model", parts=[types.Part(text=m["content"])]) for m in messages]
        self.tool = types.Tool(function_declarations=[
            types.FunctionDeclaration(name=s["name"], description=s["description"], parameters_json_schema=s["schema"]) for s in specs])

    def restrict_tools(self, specs: list[dict]) -> None:   # 검색 예산 소진 시 검색 도구를 뺀 목록으로 교체
        from google.genai import types
        self.tool = types.Tool(function_declarations=[
            types.FunctionDeclaration(name=s["name"], description=s["description"], parameters_json_schema=s["schema"]) for s in specs])

    def step(self, allow_tools: bool = True) -> Turn:
        from google.genai import errors, types

        client = providers._gemini_client()   # 변수로 붙잡아 둔다(임시 객체는 곧바로 정리되어 연결이 닫힌다)

        def call(limit: int):
            config = types.GenerateContentConfig(
                system_instruction=self.system, max_output_tokens=limit, tools=[self.tool],
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),   # 호출은 우리가 직접 실행한다
                **({} if allow_tools else {"tool_config": types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="NONE"))}))
            r = client.models.generate_content(model=self.model, contents=self.contents, config=config)
            usage.from_gemini(r)
            return r

        try:
            try:
                resp = call(providers.GEMINI_MAX_TOKENS)
            except errors.APIError as e:
                if e.code != 400 or not providers._mentions_token_limit(e.message):
                    raise
                resp = call(providers.FALLBACK_MAX_TOKENS * 2)
        except errors.APIError as e:
            if e.code in (401, 403):
                raise ChatError("Gemini 인증에 실패했습니다. .env의 GEMINI_API_KEY를 확인하세요.")
            if e.code == 404:
                raise ChatError(f"Gemini 모델을 찾을 수 없습니다: {self.model}")
            if e.code == 429:
                raise ChatError("Gemini 요청이 너무 많거나 사용 한도를 넘었습니다.")
            raise ChatError(f"Gemini 호출 실패 ({e.code}): {e.message}")
        except Exception as e:
            log.exception("Gemini 호출 중 예기치 않은 오류")
            raise ChatError(f"Gemini 서버에 연결하지 못했습니다: {type(e).__name__}")

        if not resp.candidates:
            block = getattr(resp.prompt_feedback, "block_reason", None) if resp.prompt_feedback else None
            raise ChatError("Gemini가 이 요청의 답변을 차단했습니다." if block else "빈 답변이 왔습니다. 다시 시도하세요.")
        content = resp.candidates[0].content
        parts = list(content.parts or []) if content else []
        calls = [Call(getattr(p.function_call, "id", None) or f"call_{i}", p.function_call.name, dict(p.function_call.args or {}))
                 for i, p in enumerate(p for p in parts if getattr(p, "function_call", None))]
        text = "".join(p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False)).strip()
        if calls:
            self.contents.append(content)     # 생각 서명 등을 그대로 돌려줘야 한다
        elif not text and "MAX_TOKENS" in str(getattr(resp.candidates[0], "finish_reason", "")):
            raise ChatError(providers._HIT_LIMIT)
        return Turn(text, calls)

    def add_results(self, results: list[tuple[Call, str, bool]]) -> None:
        from google.genai import types

        self.contents.append(types.Content(role="user", parts=[
            types.Part.from_function_response(name=c.name, response={"result": text} if ok else {"error": text}) for c, text, ok in results]))


_SESSIONS = {"anthropic": _Claude, "openai": _OpenAI, "google": _Gemini, "deepseek": _DeepSeek}
