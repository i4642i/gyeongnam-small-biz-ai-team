"""수집한 글을 회사 컴퓨터 문서함에 등록하는 도우미. 회사 컴퓨터의 수집 작업(app/libraries/collect.py)이 쓴다.

- 등록은 HTTP 로 자기 자신을 부르지 않는다 — 같은 프로세스 안이므로 app.rag.store/worker 를 직접 부른다.
"""

from __future__ import annotations

import hashlib
import logging

from app.rag import store as rag_store
from app.rag import worker as rag_worker

log = logging.getLogger("agent_town.rag_prep")


def _register_document(agent_id: str, filename: str, content: str, auto_index: bool = True) -> int:
    """MD 글을 이 임시 캐릭터의 문서함에 등록하고 임베딩 큐에 넣는다. app/rag/api.py upload_document() 와
    같은 절차를 HTTP 없이 직접 부른다. 문서 id 를 돌려준다."""
    data = content.encode("utf-8")
    sha = hashlib.sha256(data).hexdigest()
    dup = rag_store.find_duplicate(agent_id, sha)
    if dup:   # 같은 실행 안에서 같은 내용을 두 번 등록하려는 경우(정상적으론 안 생기지만 방어적으로)
        return dup["id"]
    doc_id, stored = rag_store.add_document(agent_id, filename, ".md", len(data), sha)
    (rag_store.docs_dir(agent_id) / stored).write_bytes(data)
    from app.libraries import indexing, store as lib_store
    if not auto_index:   # 한꺼번에 여러 건(뉴스 모으기 등) — 부르는 쪽이 다 넣은 뒤 색인 기록 하나로 줄 세운다(index_batch)
        return doc_id
    if lib_store.is_library_id(agent_id):   # 회사 컴퓨터: 색인은 컴퓨터의 "문서 색인" 도구가 있을 때만(작업 기록에 남긴다)
        indexing.on_new_document(agent_id, doc_id, requested_by="자동(새로 받은 문서)")
    else:
        rag_worker.enqueue(agent_id, doc_id)
    return doc_id
