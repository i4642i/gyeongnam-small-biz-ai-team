"""실행 취소 신호(2026-09-26). 의존성 없는 leaf 모듈 — 순환수입을 피하려고 여기 둔다.

'작업 정지'를 누르면 flow 가 지금 도는 각 책상의 run_id 를 request() 로 등록한다. 도구 루프(app/chat/tool_chat.run)는
매 라운드 시작에 is_set() 을 확인하고, 걸려 있으면 Cancelled 를 올려 '다음 모델 왕복'을 하지 않는다(진행 중이던 한 번은 끝난다).
app/runs/service._execute 가 이를 받아 실행을 'stopped' 로 마감하고 clear() 한다.
"""

import threading

_cancelled: set[str] = set()
_lock = threading.Lock()


class Cancelled(Exception):
    """사용자가 이 실행을 정지시켰다."""


def request(run_id: str) -> None:
    with _lock:
        _cancelled.add(run_id)


def is_set(run_id: str | None) -> bool:
    if not run_id:
        return False
    with _lock:
        return run_id in _cancelled


def clear(run_id: str) -> None:
    with _lock:
        _cancelled.discard(run_id)
