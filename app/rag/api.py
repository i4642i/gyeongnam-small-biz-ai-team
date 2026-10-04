"""전문자료(RAG) API: 직원 전문자료 목록·글 읽기, 컴퓨터 문서 삭제·다시 처리."""

import os

from fastapi import APIRouter, HTTPException

from app.agents import store as agents
from app.rag import embedder, search, store, worker
from app.libraries.store import is_library_id
from app.rag.extract import SUPPORTED

router = APIRouter(prefix="/api", tags=["rag"])

MAX_UPLOAD_MB = int(os.getenv("RAG_MAX_UPLOAD_MB", "30"))
MAX_DOCS = int(os.getenv("RAG_MAX_DOCS", "50"))
MAX_LIBRARY_DOCS = int(os.getenv("RAG_MAX_LIBRARY_DOCS", "500"))   # 컴퓨터는 여러 직원이 같이 쓰고 주마다 쌓이므로 캐릭터보다 넉넉하게
HIDDEN = ("stored_name", "sha256")   # 화면에 내보내지 않는 항목


def _agent_or_404(agent_id: str) -> None:
    """문서함 주인 확인: 캐릭터(a_…) 또는 컴퓨터(l_…). 아래 문서 API 는 두 경로(/agents, /libraries)에 같이 걸려 있다."""
    try:
        store.agent_dir(agent_id)
    except agents.AgentNotFound:
        raise HTTPException(404, "컴퓨터를 찾을 수 없습니다" if is_library_id(agent_id) else "에이전트를 찾을 수 없습니다")


def _max_docs(agent_id: str) -> int:
    return MAX_LIBRARY_DOCS if is_library_id(agent_id) else MAX_DOCS


def _doc_or_404(agent_id: str, doc_id: int) -> dict:
    _agent_or_404(agent_id)
    doc = store.get_document(agent_id, doc_id)
    if not doc:
        raise HTTPException(404, "문서를 찾을 수 없습니다")
    return doc


def _public(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in HIDDEN}


@router.get("/agents/{agent_id}/documents")
@router.get("/libraries/{agent_id}/documents")
def list_documents(agent_id: str):
    _agent_or_404(agent_id)
    return {
        "documents": [_public(d) for d in store.list_documents(agent_id)],
        "summary": store.summary(agent_id),
        "embedder": {"model": embedder.EMBED_MODEL, **embedder.state, "queue": worker.queue_size()},
        "stale": store.is_stale(agent_id),
        "limits": {"max_upload_mb": MAX_UPLOAD_MB, "max_docs": _max_docs(agent_id), "extensions": sorted(SUPPORTED)},
    }


@router.get("/agents/{agent_id}/documents/{doc_id}/text")
def document_text(agent_id: str, doc_id: int, start: int = 0, size: int = 20000):
    """직원 전문자료 한 건의 글 읽기(2026-10-01, 직원 소개 화면에서 서류를 눌러 볼 때). .md·.txt 는 올린 파일 그대로, PDF 는 뽑아 둔 조각 글을 이어서."""
    doc = _doc_or_404(agent_id, doc_id)
    size = max(1000, min(size, 50000))
    start = max(0, start)
    path = store.docs_dir(agent_id) / doc["stored_name"]
    if doc["ext"] in (".md", ".txt", ".markdown") and path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
    else:
        from contextlib import closing
        with closing(store._connect(agent_id)) as conn:
            text = "\n\n".join(r[0] for r in conn.execute("SELECT text FROM chunks WHERE doc_id = ? ORDER BY ord", (doc_id,)))
    return {"filename": doc["filename"], "total": len(text), "start": start, "text": text[start:start + size]}


@router.delete("/libraries/{agent_id}/documents/{doc_id}")
def delete_document(agent_id: str, doc_id: int):
    _doc_or_404(agent_id, doc_id)
    store.delete_document(agent_id, doc_id)
    search.invalidate(agent_id)
    return {"ok": True}


@router.delete("/libraries/{agent_id}/documents")
def delete_all_documents(agent_id: str):
    """회사 컴퓨터의 문서를 모두 지운다(2026-10-01). 한 건씩 지우는 것과 같은 방식이다 — 색인 조각도 함께 지워지고 되돌릴 수 없다.
    직원 전문자료에는 쓰지 못한다(컴퓨터 전용)."""
    if not is_library_id(agent_id):
        raise HTTPException(400, "회사 컴퓨터의 문서만 한꺼번에 지울 수 있습니다.")
    docs = store.list_documents(agent_id)
    for d in docs:
        store.delete_document(agent_id, d["id"])
    search.invalidate(agent_id)
    return {"ok": True, "deleted": len(docs)}


@router.post("/libraries/{agent_id}/documents/{doc_id}/retry")
def retry_document(agent_id: str, doc_id: int):
    doc = _doc_or_404(agent_id, doc_id)
    if doc["status"] not in ("failed", "stored"):
        raise HTTPException(409, "처리에 실패했거나 보관만 한 문서만 다시 처리할 수 있습니다.")
    if is_library_id(agent_id):
        from app.libraries import indexing
        if not indexing.installed(agent_id):
            raise HTTPException(409, "이 컴퓨터에 '문서 색인' 도구가 없어 색인할 수 없습니다. 설치된 도구에서 먼저 설치하세요.")
        indexing._queue(agent_id, [doc_id], "사람(다시 처리)", "다시 처리")
        return _public(store.get_document(agent_id, doc_id))
    store.update_document(agent_id, doc_id, status="queued", error=None, progress_done=0, progress_total=0)
    worker.enqueue(agent_id, doc_id)
    return _public(store.get_document(agent_id, doc_id))
