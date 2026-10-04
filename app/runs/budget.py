"""실행별 검색 예산(2026-09-26). 의존성 없는 leaf 모듈 — 순환수입을 피하려고 여기 둔다.

두 단계로 관리한다:
  - soft(권장 목표): 모델에게 "이 안에서 끝내라"고 안내하는 값(예: 60). 넘어도 코드가 막지 않는다(소프트).
  - hard(강제 한도): soft × HARD_MULT(기본 1.2배, 예: 72). 이 값에 도달하면 consume 이 False → tool_chat 이 검색 도구를 끈다.
한 실행(run) 전체에서 '검색 도구' 호출 수를 하나의 풀로 세고, 단계 전환·재시도가 바뀌어도 초기화하지 않는다.
형식 수정·잘림 복구는 도구를 끄고(use_tools=False) 돌므로 예산을 소비하지 않는다.
"""

import math
import os
import threading

HARD_MULT = float(os.getenv("RUN_SEARCH_BUDGET_HARD_MULT") or 1.2)   # 소프트 목표 대비 하드 한도 배수
# 문맥 단계(2026-09-26, 197k/200k 로 꽉 찬 채 작성을 시켜 빈 답이 난 실패 후): 사용률 60% 부터 "조사 마무리",
# 75% 에서 조회 도구를 끄고 작성으로 넘긴다. 작성용 여유(최소 3만 자, 출력 한도가 크면 그만큼)가 남도록 전환선을 계산한다.
CTX_WRAP_PCT = int(os.getenv("RUN_CTX_WRAP_PCT") or 60)
CTX_WRITE_PCT = int(os.getenv("RUN_CTX_WRITE_PCT") or 75)
WRITE_RESERVE_CHARS = int(os.getenv("RUN_CTX_WRITE_RESERVE") or 30000)
CHARS_PER_TOKEN = float(os.getenv("RUN_CTX_CHARS_PER_TOKEN") or 2.9)   # 지정학 실행 16회 호출의 대화 글자 합 ÷ 입력 토큰 합으로 잰 값
# 라운드 단계: 남은 라운드(마지막 '작성 전용' 라운드 포함) 3~5 → 마무리, 2 → 이번 라운드까지만 도구, 1 → 작성(도구 없음)
ROUNDS_WRAP = int(os.getenv("RUN_ROUNDS_WRAP") or 5)
ROUNDS_LAST_TOOL = int(os.getenv("RUN_ROUNDS_LAST_TOOL") or 2)


def write_reserve_chars(max_output_tokens: int | None) -> int:
    """작성에 남겨 둘 여유(글자). 출력 한도(토큰)를 글자로 환산한 값과 최소 여유 중 큰 쪽 — 예) 21,000토큰 → 60,900자."""
    by_output = math.ceil((max_output_tokens or 0) * CHARS_PER_TOKEN)
    return max(WRITE_RESERVE_CHARS, by_output)

_lock = threading.Lock()
_st: dict[str, dict] = {}    # run_id → {"soft": int, "hard": int, "used": int}  (검색 호출 예산)
_ctx: dict[str, dict] = {}   # run_id → {"soft": int, "hard": int, "used": int}  (모델 전달 누적 글자 예산)


def set_budget(run_id: str, soft: int, hard: int | None = None) -> None:
    if hard is None:
        hard = math.ceil(soft * HARD_MULT)
    with _lock:
        _st[run_id] = {"soft": int(soft), "hard": int(hard), "used": 0}


def _get(run_id):
    return _st.get(run_id) if run_id else None


def remaining(run_id: str | None) -> int | None:
    """소프트 기준 남은 횟수(모델 안내용). 넘어가면 0에서 멈춘다. 미설정 run 은 None(제한 없음)."""
    s = _get(run_id)
    return None if s is None else max(0, s["soft"] - s["used"])


def consume(run_id: str | None) -> bool:
    """검색 도구를 한 번 쓸 때 호출. 하드 한도 미만이면 1 세고 True, 하드 소진이면 False(=검색 그만).
    미설정(None)이면 제한 없음 → True. 소프트는 넘어도 여기서 막지 않는다(소프트)."""
    if not run_id:
        return True
    with _lock:
        s = _st.get(run_id)
        if s is None:
            return True
        if s["used"] >= s["hard"]:
            return False
        s["used"] += 1
        return True


def hard_reached(run_id: str | None) -> bool:
    """하드 한도에 도달했나(=검색 도구를 꺼야 하나)."""
    s = _get(run_id)
    return bool(s) and s["used"] >= s["hard"]


def used(run_id: str | None) -> int | None:
    s = _get(run_id)
    return None if s is None else s["used"]


def soft(run_id: str | None) -> int | None:
    s = _get(run_id)
    return None if s is None else s["soft"]


def hard(run_id: str | None) -> int | None:
    s = _get(run_id)
    return None if s is None else s["hard"]


# ---------------------------------------------------------------- 컨텍스트(대화 전체 글자) 예산 (2026-09-26)
# 검색 '횟수'와 별개로, 모델이 받는 대화 전체의 글자 수를 관리한다: 시스템 프롬프트·도구 정의·업무 입력(add_base) + 모델이 쓴 글(add_base)
# + 도구 결과(add_delivered). 처음엔 도구 결과만 셌는데, 팀장·운용은 입력(보고서·후보 목록)이 문맥의 대부분이라 점유율이 0%로 보였다.
# 정확한 토큰 수는 공급사 토크나이저가 필요하므로 보수적 대체 지표로 '문자 수'를 쓴다(토큰 수와 동일하다고 표시하지 않는다).

def set_context_budget(run_id: str, soft: int, hard: int | None = None, reserve: int | None = None) -> None:
    if hard is None:
        hard = math.ceil(soft * HARD_MULT)
    with _lock:
        _ctx[run_id] = {"soft": int(soft), "hard": int(hard), "used": 0, "last": 0,
                        "reserve": int(reserve) if reserve is not None else WRITE_RESERVE_CHARS}


def add_base(run_id: str | None, chars: int) -> None:
    """도구 결과가 아닌 대화 부분(시스템·도구 정의·입력·모델이 쓴 글)을 누적한다. '직전 라운드 크기'(last)는 건드리지 않는다."""
    if not run_id:
        return
    with _lock:
        c = _ctx.get(run_id)
        if c is not None:
            c["used"] += max(0, int(chars))


def add_delivered(run_id: str | None, chars: int) -> None:
    """모델에게 전달한 도구 결과 글자 수를 누적한다."""
    if not run_id:
        return
    with _lock:
        c = _ctx.get(run_id)
        if c is not None:
            c["used"] += max(0, int(chars))
            c["last"] = max(0, int(chars))   # 직전 라운드 전달량 — 다음 라운드가 작성 여유를 침범할지 예측하는 데 쓴다


def ctx_remaining(run_id: str | None) -> int | None:
    c = _ctx.get(run_id) if run_id else None
    return None if c is None else max(0, c["soft"] - c["used"])


def ctx_used(run_id: str | None) -> int | None:
    c = _ctx.get(run_id) if run_id else None
    return None if c is None else c["used"]


def ctx_soft(run_id: str | None) -> int | None:
    c = _ctx.get(run_id) if run_id else None
    return None if c is None else c["soft"]


def ctx_pct(run_id: str | None) -> int | None:
    """소프트 권장선 대비 사용률(%). 안내문 '권장선의 N% 사용' 용."""
    c = _ctx.get(run_id) if run_id else None
    if c is None or c["soft"] <= 0:
        return None
    return round(c["used"] * 100 / c["soft"])


def ctx_hard_reached(run_id: str | None) -> bool:
    c = _ctx.get(run_id) if run_id else None
    return bool(c) and c["used"] >= c["hard"]


def ctx_write_line(run_id: str | None) -> int | None:
    """작성 전환선(글자): 권장선의 75% 와 '권장선 − 작성 여유' 중 이른 쪽. 예산이 아주 작으면 50% 아래로는 내리지 않는다.
    예) 권장선 250,000·여유 60,900(출력 21,000토큰) → 187,500(75%) / 권장선 100,000·여유 30,000 → 70,000."""
    c = _ctx.get(run_id) if run_id else None
    if c is None or c["soft"] <= 0:
        return None
    s = c["soft"]
    return int(max(s * 0.5, min(s * CTX_WRITE_PCT / 100, s - c.get("reserve", WRITE_RESERVE_CHARS))))


def _reserve_boundary(c: dict, line: int) -> int:
    return c["soft"] - min(c.get("reserve", WRITE_RESERVE_CHARS), c["soft"] - line)


def ctx_write_guard(run_id: str | None) -> bool:
    """조회 도구를 끄고 작성으로 넘길 때인가. 누적량이 전환선에 닿았거나, 직전 라운드만큼 한 번 더 받으면
    작성 여유(권장선 − 여유)를 침범하게 될 때 True — 큰 라운드 하나로 여유를 다 먹기 전에 미리 끊는다."""
    c = _ctx.get(run_id) if run_id else None
    line = ctx_write_line(run_id)
    if c is None or line is None:
        return False
    if c.get("off"):          # 한 번 끄면 이 실행이 끝날 때까지 꺼진 상태(작은 라운드 뒤에 다시 '마무리'로 보이지 않게)
        return True
    last = c.get("last", 0)
    if c["used"] >= line or (last > 0 and c["used"] + last > _reserve_boundary(c, line)):
        c["off"] = True
        return True
    return False


def ctx_next_round_crosses(run_id: str | None) -> bool:
    """다음 라운드가 끝나면 조회 도구가 꺼질 것 같은가 — '이번 라운드까지만 도구 사용' 안내용(아직 끄지는 않는다).
    직전 라운드만큼 한 번 더 받으면 전환선(75%)에 닿거나, 두 번 더 받으면 작성 여유를 침범하는 경우. 여유선과 75% 선이
    가까울 때(출력 21,000토큰이면 189k vs 187.5k) 큰 라운드 하나로 경고 없이 바로 꺼지지 않게 한 라운드 먼저 알린다."""
    c = _ctx.get(run_id) if run_id else None
    line = ctx_write_line(run_id)
    if c is None or line is None:
        return False
    last = c.get("last", 0)
    return last > 0 and (c["used"] + last >= line or c["used"] + 2 * last > _reserve_boundary(c, line))


def clear(run_id: str) -> None:
    with _lock:
        _st.pop(run_id, None)
        _ctx.pop(run_id, None)
