"""경고 통과 허용 목록(2026-09-27 결정): 재요청으로도 안 고쳐진 '형식' 문제 중 아래 두 가지만은 결과를 버리지 않고
경고를 달아 통과시킨다. 원문과 다른 인용·빠진 핵심 내용 같은 '내용' 문제는 여기에 넣지 않는다(지금처럼 실패).

1. 서버가 대신 채우는 칸(SERVER_FILLED)이 비어 있음(null) — 서버가 실행 뒤 실제 값으로 채운다(server_fill).
2. 인용 길이 초과 중 상한(QUOTE_WORDS_CAP) 이하 — 후처리 훅이 오류 문장 앞에 [soft] 를 붙여 알린다.
   훅은 상한을 넘는 인용에는 [soft] 를 붙이지 않는다(문단 통째 인용은 여전히 실패).
"""

from __future__ import annotations

import re

SERVER_FILLED = frozenset({"written_at_kst", "filed_date", "chunk_id"})
QUOTE_WORDS_CAP = 45            # 훅이 참고하는 값(훅은 따로 실행돼 이 모듈을 import 하지 못하므로 같은 값을 적어 둔다)
TAG = "[soft]"

_CODE = re.compile(r"^\[[a-z_]+\]\s*")                                  # 표준 분류 코드([format] 등) 앞머리
_NULL = re.compile(r"^([\w/\-]+): None is not of type")                  # 스키마 오류: "evidence/3/filed_date: None is not of type 'string'"


def _strip(problem: str) -> str:
    s = str(problem or "")
    while True:
        m = _CODE.match(s)
        if not m or s.startswith(TAG):
            return s
        s = s[m.end():]


def is_soft(problem: str) -> bool:
    s = _strip(problem)
    if s.startswith(TAG):
        return True
    m = _NULL.match(s)
    return bool(m and m.group(1).split("/")[-1] in SERVER_FILLED)


LATE_TAG = "[number]"   # 숫자 변수 확인(app/runs/numbers.py) — 재요청은 하되, 재요청 뒤에도 남으면 〔확인 안 됨〕으로 채워 경고 통과


def is_late(problem: str) -> bool:
    return LATE_TAG in str(problem or "")[:60]


def all_soft(problems: list[str], final: bool = False) -> bool:
    """문제가 있고, 모두 경고 통과 대상인가. '… 외 N건' 요약 줄이 있으면(안 보이는 문제가 있으면) 아니다.
    final=True(재요청을 다 쓴 뒤 최종 판정)이면 숫자 변수 문제([number])도 경고 통과 대상이다 —
    재요청 여부를 가를 때(final=False)는 아니라서 먼저 고칠 기회를 준다(2026-09-28)."""
    ps = [str(p) for p in problems or []]
    return bool(ps) and not any(p.startswith("… 외 ") for p in ps) and all(is_soft(p) or (final and is_late(p)) for p in ps)


def label(problem: str) -> str:
    """화면·기록용: 코드·[soft] 표시를 떼고 '[경고 통과]' 를 붙인다."""
    s = _strip(problem)
    for t in (TAG, LATE_TAG):
        if s.startswith(t):
            s = s[len(t):].strip()
    return f"[경고 통과] {s}"
