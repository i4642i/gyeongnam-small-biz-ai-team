"""모델 호출의 토큰 사용량 집계.

업무 실행이 시작할 때 `start()` 로 집계 상자를 열어 두면, 그 스레드에서 일어나는 모든 모델 호출(Claude·OpenAI·Gemini)이 사용량을 여기에 더한다.
상자가 없으면(대화 등) 아무것도 하지 않는다. 금액은 계산하지 않는다(모델별 단가는 바뀌므로 공급사 콘솔의 청구 내역이 정확하다).
"""

import contextvars

_sink: contextvars.ContextVar[dict | None] = contextvars.ContextVar("usage_sink", default=None)


def start() -> dict:
    box = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
    _sink.set(box)
    return box


def add(input_tokens=0, output_tokens=0, cache_read=0, cache_write=0) -> None:
    box = _sink.get()
    if box is None:
        return
    box["calls"] += 1
    box["input_tokens"] += int(input_tokens or 0)
    box["output_tokens"] += int(output_tokens or 0)
    box["cache_read_tokens"] += int(cache_read or 0)
    box["cache_write_tokens"] += int(cache_write or 0)


def note(**kv) -> None:
    """이 실행에 대한 메타 기록을 usage box 에 남긴다(run["usage"] 로 저장·표시됨).
    applied_max_tokens·finish_reason 는 마지막 모델 호출 값을 기록(뒤가 최종 답변),
    output_limit_reduced 는 한 번이라도 낮아졌으면 유지(sticky True)."""
    box = _sink.get()
    if box is None:
        return
    for k, v in kv.items():
        if k == "output_limit_reduced":
            box[k] = box.get(k, False) or bool(v)
        else:
            box[k] = v


def from_claude(resp) -> None:
    u = getattr(resp, "usage", None)
    if u is not None:
        add(getattr(u, "input_tokens", 0), getattr(u, "output_tokens", 0), getattr(u, "cache_read_input_tokens", 0), getattr(u, "cache_creation_input_tokens", 0))


def _openai_cache_hit(u) -> int:
    """캐시 적중 토큰 — DeepSeek 은 prompt_cache_hit_tokens, OpenAI 는 prompt_tokens_details.cached_tokens.
    예전엔 읽지 않아 DeepSeek 캐시 읽기가 늘 0 으로 기록됐다(2026-09-28, 숙제 B3). 입력(prompt_tokens)에는 적중분이 포함돼 있다."""
    hit = getattr(u, "prompt_cache_hit_tokens", None)
    if hit is None:
        extra = getattr(u, "model_extra", None) or {}
        hit = extra.get("prompt_cache_hit_tokens")
    if hit is None:
        details = getattr(u, "prompt_tokens_details", None)
        hit = getattr(details, "cached_tokens", None) if details is not None else None
    try:
        return int(hit or 0)
    except (TypeError, ValueError):
        return 0


def from_openai(resp) -> None:
    u = getattr(resp, "usage", None)
    if u is not None:
        add(getattr(u, "prompt_tokens", 0), getattr(u, "completion_tokens", 0), _openai_cache_hit(u))


def from_gemini(resp) -> None:
    u = getattr(resp, "usage_metadata", None)
    if u is not None:
        add(getattr(u, "prompt_token_count", 0), getattr(u, "candidates_token_count", 0))
