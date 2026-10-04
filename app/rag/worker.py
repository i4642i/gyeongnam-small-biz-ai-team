"""문서 처리를 백그라운드에서 한 개씩 순서대로 한다: 읽기 → 조각내기 → 임베딩 → 벡터 저장.

임베딩이 CPU 를 거의 다 쓰기 때문에 여러 문서를 동시에 돌리지 않고 한 줄로 세운다.
서버가 꺼졌다 켜지면 처리 중이던 문서는 처음부터 다시 처리한다(recover). 처리 도중 문서나 캐릭터가 삭제되면 멈춘다.
"""

import logging
import queue
import threading
import time

from app.agents import store as agents
from app.config import AGENTS_DIR, LIBRARIES_DIR
from app.rag import chunking, embedder, extract, graph, search, store

log = logging.getLogger("agent_town.worker")

STEP = 64   # 조각을 이만큼 임베딩할 때마다 진행 상황을 저장하고, 삭제 여부를 확인한다(2026-09-22: 16→64, embedder.BATCH_SIZE 32와 맞춰 배치를 키움)

_queue: queue.Queue = queue.Queue()
_thread: threading.Thread | None = None
_start_lock = threading.Lock()
_current: tuple[str, int] | None = None        # 지금 처리 중인 (캐릭터 id, 문서 id)
_aborted: set[str] = set()                     # 삭제 중인 캐릭터


def enqueue(agent_id: str, doc_id: int) -> None:
    global _thread
    with _start_lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="rag-worker", daemon=True)
            _thread.start()
    _queue.put((agent_id, doc_id))


def snapshot() -> list[tuple[str, int]]:
    """색인 줄의 지금 모습: [(주인 id, 문서 id)] — 첫 항목이 지금 처리 중(있으면). 화면에 순번을 보여 주는 데만 쓴다."""
    waiting = list(_queue.queue)   # 줄을 건드리지 않고 복사만 한다
    return ([_current] if _current else []) + waiting


def queue_size() -> int:
    return _queue.qsize() + (1 if _current else 0)


def _loop() -> None:
    global _current
    while True:
        agent_id, doc_id = _queue.get()
        try:
            _process(agent_id, doc_id)
        except Exception:
            log.exception("문서 처리 루프에서 예기치 않은 오류 (%s, %s)", agent_id, doc_id)
        finally:
            _current = None


def _process(agent_id: str, doc_id: int) -> None:
    global _current
    if agent_id in _aborted:
        return
    try:
        doc = store.get_document(agent_id, doc_id)
    except agents.AgentNotFound:
        return
    if not doc:
        return
    _current = (agent_id, doc_id)

    def gone() -> bool:
        """처리 도중 문서나 캐릭터가 삭제됐는가."""
        return agent_id in _aborted or store.get_document(agent_id, doc_id) is None

    try:
        store.update_document(agent_id, doc_id, status="extracting", error=None, progress_done=0, progress_total=0)
        pages, empty = extract.extract(store.docs_dir(agent_id) / doc["stored_name"], doc["ext"])
        chunks = chunking.make_chunks(pages)
        if not chunks:
            store.update_document(agent_id, doc_id, status="failed", error="문서에서 검색할 만한 글을 찾지 못했습니다.")
            return
        store.delete_vectors(agent_id, doc_id)   # 다시 처리하는 경우 이전 벡터를 먼저 정리한다
        ids = store.replace_chunks(agent_id, doc_id, chunks)
        meta = chunking.doc_meta(pages[0].text) if pages else {}   # 티커·문서 종류·기간(RAG 개선 4번, 있으면만)
        date_keys = ("published_at", "date_status", "date_basis", "date_evidence", "event_date")
        date_meta = {k: meta.pop(k) for k in date_keys if k in meta}
        if not store.update_document(agent_id, doc_id, status="embedding", pages=len(pages), chunks=len(chunks),
                                     empty_pages=empty, progress_done=0, progress_total=len(chunks), **meta):
            return   # 그 사이 삭제됨
        if date_meta:   # 게재일 관련 필드는 별도 규칙(이미 confirmed 면 안 덮어씀)으로 갱신한다(app/rag/store.py update_date_meta)
            store.update_date_meta(agent_id, doc_id, **date_meta)

        for start in range(0, len(ids), STEP):
            if gone():
                store.delete_vectors(agent_id, doc_id)
                return
            texts = [t for _, t in chunks[start:start + STEP]]
            vectors = embedder.embed(texts)
            store.add_vectors(agent_id, [
                {"id": cid, "doc_id": doc_id, "vector": v.tolist()} for cid, v in zip(ids[start:start + STEP], vectors)
            ])
            store.update_document(agent_id, doc_id, progress_done=min(start + STEP, len(ids)))

        if gone():
            store.delete_vectors(agent_id, doc_id)
            return
        store.set_meta(agent_id, "embed_model", embedder.EMBED_MODEL)
        store.update_document(agent_id, doc_id, status="ready", progress_done=len(ids))
        search.invalidate(agent_id)
        from app.rag import graph_table
        if graph_table.is_table_doc(doc["filename"]):   # 지식 표(kg_*)는 색인이 끝나면 바로 그래프에 넣는다(모델 호출 없음)
            try:
                graph_table.import_document(agent_id, doc_id)
            except Exception:
                log.exception("지식 표 넣기 실패 (%s, %s)", agent_id, doc_id)
        log.info("문서 처리 완료: %s #%s (%d쪽, %d조각)", agent_id, doc_id, len(pages), len(chunks))
    except extract.ExtractError as e:
        store.update_document(agent_id, doc_id, status="failed", error=str(e))
    except agents.AgentNotFound:
        pass
    except Exception as e:
        log.exception("문서 처리 실패 (%s, %s)", agent_id, doc_id)
        try:
            store.update_document(agent_id, doc_id, status="failed", error=f"처리 중 오류가 났습니다: {type(e).__name__}: {e}")
        except Exception:
            pass


def abort_agent(agent_id: str, timeout: float = 60.0) -> None:
    """캐릭터를 지우기 전에 부른다: 이 캐릭터의 문서 처리를 멈추고, 진행 중인 작업이 끝날 때까지 기다린다.
    (처리 중인 파일이 열려 있으면 윈도우에서 폴더를 지울 수 없다.)"""
    _aborted.add(agent_id)
    search.invalidate(agent_id)
    graph.abort_agent(agent_id, timeout)
    deadline = time.time() + timeout
    while _current and _current[0] == agent_id and time.time() < deadline:
        time.sleep(0.2)


def finish_abort(agent_id: str) -> None:
    _aborted.discard(agent_id)
    graph.finish_abort(agent_id)


def recover() -> None:
    """서버를 켤 때: 처리가 끝나지 않은 문서를 처음부터 다시 처리하도록 줄 세운다."""
    for d in [*sorted(AGENTS_DIR.iterdir()), *sorted(LIBRARIES_DIR.iterdir())]:   # 캐릭터 문서함과 컴퓨터
        if not (d.is_dir() and (d / "agent.db").is_file()):
            continue
        try:
            for doc in store.list_documents(d.name):
                if doc["status"] in store.PROCESSING:
                    store.update_document(d.name, doc["id"], status="queued", progress_done=0)
                    enqueue(d.name, doc["id"])
                    log.info("처리가 끝나지 않은 문서를 다시 줄 세웁니다: %s #%s", d.name, doc["id"])
        except Exception:
            log.warning("문서 복구 중 오류: %s", d, exc_info=True)
