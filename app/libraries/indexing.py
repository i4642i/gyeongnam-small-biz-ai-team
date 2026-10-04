"""회사 컴퓨터의 "문서 색인" 도구. 컴퓨터에 이 도구가 설치돼 있어야 받은 문서를 색인한다(설치 안 했으면 '보관만').

색인 계산 자체는 이 PC 의 색인 일꾼(app/rag/worker, 모든 컴퓨터·직원 문서함이 함께 쓰는 한 줄 대기열)이 한다.
이 모듈은 "어느 컴퓨터가 어떤 문서를 색인하라고 줄 세웠는지"를 컴퓨터의 작업 기록에 남기고, 도구 설치 여부를 지킨다.
"""

from __future__ import annotations

from app.libraries import store
from app.rag import search, store as rag_store, worker

TOOL_ID = "rag_index"
STORED = "stored"   # 색인 도구가 없어 보관만 한 문서(검색 후보가 아니다 — status 가 ready 가 아니므로)


def installed(library_id: str) -> bool:
    return TOOL_ID in store.get(library_id).get("tools", [])


def on_new_document(library_id: str, doc_id: int, requested_by: str) -> bool:
    """컴퓨터에 새 문서가 들어왔을 때: 색인 도구가 있으면 색인 줄에 넣고 작업으로 남긴다. 없으면 '보관만'으로 둔다."""
    if not installed(library_id):
        rag_store.update_document(library_id, doc_id, status=STORED, error=None)
        return False
    _queue(library_id, [doc_id], requested_by, "새 문서 색인")
    return True


def index_batch(library_id: str, doc_ids: list[int], requested_by: str) -> bool:
    """새 문서 여러 건을 색인 기록 하나로 줄 세운다(2026-09-29 — 뉴스 200건이 기록 200줄로 쌓이던 문제). 색인 도구가 없으면 보관만."""
    if not doc_ids:
        return False
    if not installed(library_id):
        for d in doc_ids:
            rag_store.update_document(library_id, d, status=STORED, error=None)
        return False
    _queue(library_id, doc_ids, requested_by, f"새 문서 색인 {len(doc_ids)}건")
    return True


def enqueue_docs(library_id: str, doc_ids: list[int]) -> None:
    for d in doc_ids:
        rag_store.update_document(library_id, d, status="queued", error=None, progress_done=0, progress_total=0)
        worker.enqueue(library_id, d)


def _queue(library_id: str, doc_ids: list[int], requested_by: str, reason: str) -> dict:
    from app.libraries import collect
    docs = [d for d in (rag_store.get_document(library_id, x) for x in doc_ids) if d]
    enqueue_docs(library_id, [d["id"] for d in docs])
    return collect.record(library_id, TOOL_ID, {"문서": ", ".join(d["filename"] for d in docs)[:300]}, requested_by, reason,
                          documents=[{"doc_id": d["id"], "filename": d["filename"]} for d in docs])


def select(library_id: str, target: str) -> list[dict]:
    """도구를 실행할 때 색인할 문서: '새 문서만'(보관만·실패한 문서) 또는 '전체 다시'(색인을 지우고 모든 문서)."""
    docs = rag_store.list_documents(library_id)
    if target == "전체 다시":
        rag_store.drop_index(library_id)
        search.invalidate(library_id)
        return docs
    return [d for d in docs if d["status"] in (STORED, "failed")]


def run(library_id: str, target: str, requested_by: str) -> dict:
    docs = select(library_id, target)
    if not docs:
        from app.libraries import collect
        return collect.record(library_id, TOOL_ID, {"target": target}, requested_by, "", notes=["색인할 문서가 없습니다."])
    return _queue(library_id, [d["id"] for d in docs], requested_by, target)


def queue_position(library_id: str) -> dict[int, int]:
    """색인 일꾼 줄에서 이 컴퓨터 문서의 순번(1 = 지금 처리 중). 줄은 모든 컴퓨터·직원 문서함이 함께 쓴다."""
    out = {}
    for i, (owner, doc_id) in enumerate(worker.snapshot(), start=1):
        if owner == library_id and doc_id not in out:
            out[doc_id] = i
    return out
