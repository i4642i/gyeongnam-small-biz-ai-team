"""문서·조각 저장소. 캐릭터 폴더 안에 둔다 (README '폴더 구조' 참고).

data/agents/a_xxxxxxxx/
    docs/        업로드한 원본 파일 (d{번호}.pdf 처럼 안전한 이름으로 저장, 원래 이름은 DB에)
    agent.db     documents(문서 상태), chunks(조각 글·쪽수), rag_meta — 대화 기록(messages)과 같은 파일
    index/       LanceDB: 조각 id → 벡터. 글은 여기 없고 agent.db 에 있다 (벡터는 docs/ 로 언제든 다시 만들 수 있다)
"""

import hashlib
import json
import re
import shutil
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import threading

from app.agents.store import agent_dir as _agent_dir
from app.libraries.store import is_library_id, library_dir
from app.rag import embedder


def agent_dir(owner_id: str) -> Path:
    """문서함 주인 폴더: 캐릭터(a_…) 또는 컴퓨터(l_…). 이 모듈의 모든 함수가 이걸로 경로를 찾는다."""
    return library_dir(owner_id) if is_library_id(owner_id) else _agent_dir(owner_id)


PROCESSING = ("queued", "extracting", "embedding")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    filename       TEXT NOT NULL,
    stored_name    TEXT NOT NULL DEFAULT '',
    ext            TEXT NOT NULL,
    size           INTEGER NOT NULL,
    sha256         TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'queued',   -- queued | extracting | embedding | ready | failed
    progress_done  INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    pages          INTEGER,
    chunks         INTEGER NOT NULL DEFAULT 0,
    empty_pages    INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    ticker         TEXT,   -- 문서 맨 앞 정보표(티커·문서 종류·기간)에서 읽음(app/rag/chunking.py _parse_front_matter). 없으면 NULL
    doc_type       TEXT,   -- 예: 10-K, 10-Q, Earnings Call Transcript
    period         TEXT,   -- 예: FY2025, Q2 2026
    published_at   TEXT,   -- 정확한 공표일 YYYY-MM-DD. 미상은 NULL(기준일 검색은 이 칼럼만 본다)
    date_status    TEXT NOT NULL DEFAULT 'unknown',  -- confirmed | needs_review | unknown — 문서 처리 status(위)와 별개(2026-09-23)
    date_basis     TEXT,   -- sec_filing_date | document_stated | source_page | manual — "이 날짜를 어떻게 아는가"
    date_evidence  TEXT,   -- JSON: {accession, filing_url, document_url, exhibit_type, basis_text, source_url, reason(확인 필요 사유)}
    date_confirmed_at TEXT, -- date_status가 confirmed 가 된 시각
    event_date     TEXT,   -- 게재일과 다를 수 있는 문서 안 사건 날짜(예: 실적 발표 통화 날짜). 기준일 검색에는 안 쓴다
    search_excluded INTEGER NOT NULL DEFAULT 0,  -- 1이면 검색 후보에서 뺀다(보존은 함, 삭제 아님). 문서 단위 개별 지정만 — 상태값 기준 일괄 정책 아님(2026-09-23)
    excluded_reason TEXT,   -- search_excluded=1 인 이유(사람이 남김)
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL,
    ord    INTEGER NOT NULL,
    page   INTEGER,
    text   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks (doc_id);
CREATE TABLE IF NOT EXISTS rag_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- 지식 그래프 (app/rag/graph.py). 조각을 지우면 여기서도 함께 지운다(purge_graph).
CREATE TABLE IF NOT EXISTS graph_chunks (chunk_id INTEGER PRIMARY KEY, ok INTEGER NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS entities (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    norm TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    desc TEXT NOT NULL DEFAULT ''      -- 문서에서 뽑은 한 줄 설명(속성). 예전에 만든 그래프에는 없다(아래 _connect 가 열을 더한다)
);
-- 도구(웹 검색 등) 하루 사용 횟수: 캐릭터마다, 도구마다. 하루 한도를 넘으면 도구를 부르지 않고 안내만 돌려준다.
CREATE TABLE IF NOT EXISTS tool_usage (day TEXT NOT NULL, tool_id TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, tool_id));
-- 개체의 의미 벡터(이름 + 설명을 임베딩). 질문 표현이 이름과 달라도 의미로 개체를 찾는 데 쓴다. 필요할 때 만들고, 개체가 바뀌면 지운다.
CREATE TABLE IF NOT EXISTS entity_vecs (entity_id INTEGER PRIMARY KEY, vec BLOB NOT NULL, model TEXT NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entity_chunks (entity_id INTEGER NOT NULL, chunk_id INTEGER NOT NULL, PRIMARY KEY (entity_id, chunk_id));
CREATE INDEX IF NOT EXISTS entity_chunks_chunk ON entity_chunks (chunk_id);
CREATE TABLE IF NOT EXISTS relations (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    src      INTEGER NOT NULL,
    dst      INTEGER NOT NULL,
    label    TEXT NOT NULL,
    chunk_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS relations_chunk ON relations (chunk_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(agent_id: str) -> sqlite3.Connection:
    # 문서 처리(백그라운드)와 요청이 같은 파일을 동시에 쓰므로, 잠겨 있으면 기다린다
    conn = sqlite3.connect(agent_dir(agent_id) / "agent.db", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    if "desc" not in {r[1] for r in conn.execute("PRAGMA table_info(entities)")}:   # 설명 기능이 생기기 전에 만든 그래프
        try:
            conn.execute("ALTER TABLE entities ADD COLUMN desc TEXT NOT NULL DEFAULT ''")
            conn.commit()
        except sqlite3.OperationalError:   # 다른 연결이 방금 더했다
            pass
    doc_cols = {r[1] for r in conn.execute("PRAGMA table_info(documents)")}   # 티커·문서 종류·기간 필터(RAG 개선 4번)가 생기기 전에 만든 DB
    for col in ("ticker", "doc_type", "period", "published_at", "date_basis", "date_evidence", "date_confirmed_at", "event_date", "excluded_reason"):
        if col not in doc_cols:
            try:
                conn.execute(f"ALTER TABLE documents ADD COLUMN {col} TEXT")
                conn.commit()
            except sqlite3.OperationalError:
                pass
    if "date_status" not in doc_cols:
        try:
            conn.execute("ALTER TABLE documents ADD COLUMN date_status TEXT NOT NULL DEFAULT 'unknown'")
            conn.execute("UPDATE documents SET date_status = 'confirmed' WHERE published_at IS NOT NULL AND date_status = 'unknown'")
            conn.commit()
        except sqlite3.OperationalError:
            pass
    if "search_excluded" not in doc_cols:
        try:
            conn.execute("ALTER TABLE documents ADD COLUMN search_excluded INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        except sqlite3.OperationalError:
            pass
    return conn


def docs_dir(agent_id: str) -> Path:
    d = agent_dir(agent_id) / "docs"
    d.mkdir(exist_ok=True)
    return d


# ---------------------------------------------------------------- 문서

def add_document(agent_id: str, filename: str, ext: str, size: int, sha256: str) -> tuple[int, str]:
    """문서 행을 만들고 (id, 저장할 파일 이름)을 돌려준다."""
    with closing(_connect(agent_id)) as conn, conn:
        cur = conn.execute(
            "INSERT INTO documents (filename, ext, size, sha256, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (filename, ext, size, sha256, _now(), _now()),
        )
        doc_id = cur.lastrowid
        stored = f"d{doc_id}{ext}"
        conn.execute("UPDATE documents SET stored_name = ? WHERE id = ?", (stored, doc_id))
    return doc_id, stored


def get_document(agent_id: str, doc_id: int) -> dict | None:
    with closing(_connect(agent_id)) as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return dict(row) if row else None


def list_documents(agent_id: str) -> list[dict]:
    with closing(_connect(agent_id)) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM documents ORDER BY id")]


def find_duplicate(agent_id: str, sha256: str) -> dict | None:
    with closing(_connect(agent_id)) as conn:
        row = conn.execute("SELECT * FROM documents WHERE sha256 = ? AND status != 'failed' LIMIT 1", (sha256,)).fetchone()
    return dict(row) if row else None


def update_document(agent_id: str, doc_id: int, **fields) -> bool:
    """문서 행을 고친다. 그 사이 문서가 삭제됐으면 False (처리 중이던 작업은 이걸 보고 멈춘다)."""
    cols = ", ".join(f"{k} = ?" for k in fields)
    with closing(_connect(agent_id)) as conn, conn:
        cur = conn.execute(f"UPDATE documents SET {cols}, updated_at = ? WHERE id = ?", (*fields.values(), _now(), doc_id))
        return cur.rowcount > 0


def update_date_meta(agent_id: str, doc_id: int, *, published_at: str | None = None, date_status: str | None = None,
                     date_basis: str | None = None, date_evidence: str | None = None, event_date: str | None = None) -> bool:
    """게재일 관련 필드만 규칙대로 갱신한다(2026-09-23, update_document 와 분리):
    - date_status 가 이미 'confirmed' 면 published_at·date_basis·date_evidence·date_status 를 절대 덮어쓰지 않는다
      (확인된 날짜는 보존 — 근거 없이 덮어쓰지 말라는 요구).
    - 'unknown'·'needs_review' 상태에서는 새로 들어온 값으로 갱신한다(근거가 생기면 confirmed 로 올라가고,
      여전히 애매하면 needs_review 로 남는다 — date_evidence 의 최신 사유로 덮어쓴다. 지난 시도 이력은 안 쌓는다).
    - event_date(사건 날짜, 예: 실적 통화 날짜)는 게재일과 별개 참고 필드라 항상 최신값으로 갱신한다."""
    with closing(_connect(agent_id)) as conn, conn:
        row = conn.execute("SELECT date_status FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            return False
        fields: dict = {}
        if (row["date_status"] or "unknown") != "confirmed":
            if date_status is not None:
                fields["date_status"] = date_status
                fields["date_confirmed_at"] = _now() if date_status == "confirmed" else None
            if published_at is not None:
                fields["published_at"] = published_at
            if date_basis is not None:
                fields["date_basis"] = date_basis
            if date_evidence is not None:
                fields["date_evidence"] = date_evidence
        if event_date is not None:
            fields["event_date"] = event_date
        if not fields:
            return True
        cols = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE documents SET {cols}, updated_at = ? WHERE id = ?", (*fields.values(), _now(), doc_id))
        return True


def confirm_date(agent_id: str, doc_id: int, *, published_at: str, date_basis: str, date_evidence: dict | str,
                 event_date: str | None = None) -> bool:
    """검증된 날짜를 명시적으로 확정한다 — update_date_meta() 의 "이미 confirmed 면 보호" 규칙을 우회하는
    유일한 통로(2026-09-23). 호출자가 실제로 원 출처와 대조해 확인했을 때만 불러야 한다.
    date_basis 와, date_evidence 안의 (accession+document_filename) 또는 source_url 이 없으면 거부한다 —
    "헤더에 뭔가 적혀 있다"는 사실만으로 confirmed 를 주지 않기 위한 코드 수준 강제. 기존 값을 정정하는
    경우 호출자가 date_evidence 에 {"correction": {"old_published_at":..., "reason":...}} 를 넣어 남긴다."""
    ev = date_evidence if isinstance(date_evidence, dict) else json.loads(date_evidence)
    if not date_basis:
        raise ValueError("date_basis 없이는 confirmed 로 확정할 수 없습니다.")
    if not ((ev.get("accession") and ev.get("document_filename")) or ev.get("source_url")):
        raise ValueError("date_evidence 에 (accession+document_filename) 또는 source_url 이 있어야 confirmed 로 확정할 수 있습니다.")
    with closing(_connect(agent_id)) as conn, conn:
        if conn.execute("SELECT id FROM documents WHERE id = ?", (doc_id,)).fetchone() is None:
            return False
        conn.execute("UPDATE documents SET published_at = ?, date_status = 'confirmed', date_basis = ?, "
                     "date_evidence = ?, date_confirmed_at = ?, event_date = COALESCE(?, event_date), updated_at = ? WHERE id = ?",
                     (published_at, date_basis, json.dumps(ev, ensure_ascii=False), _now(), event_date, _now(), doc_id))
        return True


_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|\s*$", re.M)
_FM_END_RE = re.compile(r"\n-{3,}\s*\n")
_ROW_ORDER = ("티커", "회사", "문서 종류", "기간", "보고 기간 끝", "게재일", "게재일 근거", "접수번호", "문서파일", "원문", "통화 날짜")


def sync_header_rows(agent_id: str, doc_id: int, new_rows: dict) -> dict:
    """문서 정보표에 줄을 추가·수정하고(예: 게재일 근거·접수번호·문서파일), 그 표가 든 조각의 텍스트·벡터만
    다시 계산한다(본문은 그대로, 전체 재임베딩 없음, 2026-09-23). new_rows 에 없는 기존 값은 보존한다.
    반환: {"size", "sha256", "chunk_updated": bool}. 정보표를 못 찾으면(문서 구조가 다르면) chunk_updated=False."""
    doc = get_document(agent_id, doc_id)
    if doc is None:
        raise ValueError(f"문서를 찾을 수 없습니다: {doc_id}")
    path = docs_dir(agent_id) / doc["stored_name"]
    raw = path.read_text(encoding="utf-8")
    m = _FM_END_RE.search(raw)
    head = raw[: m.start()] if m else raw
    body = raw[m.end():] if m else ""
    rows = {k: v for k, v in _ROW_RE.findall(head) if k not in ("항목", "---")}
    rows.update({k: v for k, v in new_rows.items() if v is not None})
    ordered = [(k, rows[k]) for k in _ROW_ORDER if k in rows]
    ordered += [(k, v) for k, v in rows.items() if k not in _ROW_ORDER]   # 모르는 키도 잃지 않는다
    table = "| 항목 | 값 |\n|---|---|\n" + "\n".join(f"| {k} | {v} |" for k, v in ordered)
    new_head = table + ("\n\n---\n\n" if m else "\n\n")
    new_raw = new_head + body
    if new_raw == raw:
        return {"size": doc["size"], "sha256": doc["sha256"], "chunk_updated": False}

    with closing(_connect(agent_id)) as conn:
        chunk = conn.execute("SELECT id, text FROM chunks WHERE doc_id = ? AND text LIKE '%게재일%' ORDER BY ord LIMIT 1",
                             (doc_id,)).fetchone()
    chunk_updated = False
    if chunk is not None:
        old_chunk_text = chunk["text"]
        # 조각 글은 문서 원문과 똑같지 않다 — 청킹이 앞에 "[티커 · 문서종류 · 기간 · 구역제목]" 머리표를 붙이고,
        # 표 뒤 구분선("---")·빈 줄은 잘라낸다(app/rag/chunking.py make_chunks). 그래서 "| 항목"이 어디서
        # 시작하는지 찾아 그 지점부터(있으면 다음 빈 줄까지, 없으면 조각 끝까지)만 새 표로 바꿔치기한다 —
        # 머리표·그 뒤에 이어질 수 있는 다른 내용은 그대로 둔다.
        start = old_chunk_text.find("| 항목")
        if start == -1:
            new_chunk_text = old_chunk_text   # 예상과 다른 구조면 손대지 않는다(안전 우선)
        else:
            blank = old_chunk_text.find("\n\n", start)
            end = blank if blank != -1 else len(old_chunk_text)
            new_chunk_text = old_chunk_text[:start] + table + old_chunk_text[end:]
        if new_chunk_text != old_chunk_text:
            vector = embedder.embed([new_chunk_text])[0]
            table_obj = _table(agent_id)
            with closing(_connect(agent_id)) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                table_obj.update(where=f"id = {int(chunk['id'])}", values={"vector": vector.tolist()})
                conn.execute("UPDATE chunks SET text = ? WHERE id = ?", (new_chunk_text, chunk["id"]))
            chunk_updated = True

    path.write_text(new_raw, encoding="utf-8")
    size, sha = len(new_raw.encode("utf-8")), hashlib.sha256(new_raw.encode("utf-8")).hexdigest()
    update_document(agent_id, doc_id, size=size, sha256=sha)
    return {"size": size, "sha256": sha, "chunk_updated": chunk_updated}


def exclude_from_search(agent_id: str, doc_id: int, reason: str) -> bool:
    """문서를 지우지 않고 검색 후보에서만 뺀다(2026-09-23). 반드시 이 문서 하나를 콕 집어 부르는 곳에서만
    쓴다 — needs_review 등 상태값으로 한꺼번에 걸러내는 일반 정책으로 쓰면 안 된다(요청 사항).
    reason 은 사람이 남기는 사유(예: '다른 공시로 확인됨, 대체 문서 등록 전까지 검색 제외')."""
    if not reason:
        raise ValueError("excluded_reason 없이는 검색 제외할 수 없습니다(사유를 남겨야 합니다).")
    return update_document(agent_id, doc_id, search_excluded=1, excluded_reason=reason)


def include_in_search(agent_id: str, doc_id: int) -> bool:
    """검색 제외를 되돌린다."""
    return update_document(agent_id, doc_id, search_excluded=0, excluded_reason=None)


def summary(agent_id: str) -> dict:
    """목록 카드에 보여 줄 문서 개수. 문서를 한 번도 안 올린 캐릭터는 DB 를 만들지 않고 0 을 돌려준다."""
    empty = {"total": 0, "ready": 0, "processing": 0, "failed": 0}
    if not (agent_dir(agent_id) / "agent.db").is_file():
        return empty
    with closing(_connect(agent_id)) as conn:
        for status, n in conn.execute("SELECT status, COUNT(*) FROM documents GROUP BY status"):
            empty["total"] += n
            key = "ready" if status == "ready" else "failed" if status == "failed" else "processing"
            empty[key] += n
    return empty


def has_ready(agent_id: str) -> bool:
    return summary(agent_id)["ready"] > 0


# ---------------------------------------------------------------- 조각

def purge_graph(conn: sqlite3.Connection, doc_id: int) -> None:
    """문서의 조각에서 뽑은 지식 그래프 항목을 지운다. 조각을 지우기 *전에*, 같은 트랜잭션 안에서 부른다.
    어떤 조각에도 나오지 않게 된 개체는 함께 지운다."""
    ids = "SELECT id FROM chunks WHERE doc_id = ?"
    conn.execute(f"DELETE FROM graph_chunks WHERE chunk_id IN ({ids})", (doc_id,))
    conn.execute(f"DELETE FROM relations WHERE chunk_id IN ({ids})", (doc_id,))
    conn.execute(f"DELETE FROM entity_chunks WHERE chunk_id IN ({ids})", (doc_id,))
    conn.execute("DELETE FROM entities WHERE id NOT IN (SELECT entity_id FROM entity_chunks)")
    conn.execute("DELETE FROM entity_vecs WHERE entity_id NOT IN (SELECT id FROM entities)")


def clear_graph(agent_id: str) -> None:
    with closing(_connect(agent_id)) as conn, conn:
        for table in ("graph_chunks", "relations", "entity_chunks", "entities", "entity_vecs"):
            conn.execute(f"DELETE FROM {table}")


def replace_chunks(agent_id: str, doc_id: int, chunks: list[tuple[int | None, str]]) -> list[int]:
    """문서의 조각을 통째로 바꾸고 새 조각 id 들을 순서대로 돌려준다."""
    with closing(_connect(agent_id)) as conn, conn:
        purge_graph(conn, doc_id)   # 조각 id 가 바뀌므로 이 문서에서 뽑은 그래프도 다시 만들어야 한다
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        ids = []
        for i, (page, text) in enumerate(chunks):
            ids.append(conn.execute("INSERT INTO chunks (doc_id, ord, page, text) VALUES (?, ?, ?, ?)", (doc_id, i, page, text)).lastrowid)
    return ids


def chunk_rows(agent_id: str, ids: list[int]) -> dict[int, dict]:
    """조각 id → 조각 + 문서 정보. 처리가 끝난(ready) 문서의 조각만 돌려준다."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    with closing(_connect(agent_id)) as conn:
        rows = conn.execute(
            f"SELECT c.id, c.doc_id, c.ord, c.page, c.text, d.filename, d.published_at FROM chunks c JOIN documents d ON d.id = c.doc_id "
            f"WHERE c.id IN ({marks}) AND d.status = 'ready'", ids,
        ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def doc_ids_matching(agent_id: str, ticker: str | None = None, doc_type: str | None = None, as_of: str | None = None) -> set[int]:
    """티커·문서 종류로 거를 때(RAG 개선 4번) 대상이 되는 문서 id. 값을 안 주면 그 조건은 안 건다."""
    conds, params = ["status = 'ready'", "search_excluded = 0"], []
    if ticker:
        conds.append("UPPER(ticker) = ?")
        params.append(ticker.upper())
    if doc_type:
        conds.append("doc_type = ?")
        params.append(doc_type)
    if as_of:
        from app.rag.scope import iso_date
        conds.append("published_at IS NOT NULL AND length(published_at) = 10 AND published_at <= ?")
        params.append(iso_date(as_of))
    with closing(_connect(agent_id)) as conn:
        rows = conn.execute(f"SELECT id FROM documents WHERE {' AND '.join(conds)}", params).fetchall()
    return {r["id"] for r in rows}


def chunk_ids_for_docs(agent_id: str, doc_ids: set[int]) -> set[int]:
    if not doc_ids:
        return set()
    ids = sorted(int(x) for x in doc_ids)
    with closing(_connect(agent_id)) as conn:
        return {r[0] for start in range(0, len(ids), 500) for r in conn.execute(
            f"SELECT id FROM chunks WHERE doc_id IN ({','.join('?' for _ in ids[start:start + 500])})",
            ids[start:start + 500])}


def chunk_doc_ids(agent_id: str, ids: list[int]) -> dict[int, int]:
    """조각 id → 그 문서 id (필터링에 쓴다)."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    with closing(_connect(agent_id)) as conn:
        rows = conn.execute(f"SELECT id, doc_id FROM chunks WHERE id IN ({marks})", ids).fetchall()
    return {r["id"]: r["doc_id"] for r in rows}


def ready_chunk_texts(agent_id: str) -> list[tuple[int, str]]:
    with closing(_connect(agent_id)) as conn:
        return [(r["id"], r["text"]) for r in conn.execute(
            "SELECT c.id, c.text FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE d.status = 'ready' ORDER BY c.id")]


def ready_signature(agent_id: str) -> tuple[int, int]:
    """검색용 키워드 색인을 다시 만들어야 하는지 알아보는 표지: (조각 수, 가장 큰 조각 id)."""
    with closing(_connect(agent_id)) as conn:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(c.id), 0) FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE d.status = 'ready'").fetchone()
    return (row[0], row[1])


def get_meta(agent_id: str, key: str) -> str | None:
    with closing(_connect(agent_id)) as conn:
        row = conn.execute("SELECT value FROM rag_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(agent_id: str, key: str, value: str) -> None:
    with closing(_connect(agent_id)) as conn, conn:
        conn.execute("INSERT INTO rag_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def is_stale(agent_id: str) -> bool:
    """색인을 만들 때 쓴 임베딩 모델이 지금 설정과 다르면 True (벡터를 다시 만들어야 한다)."""
    used = get_meta(agent_id, "embed_model")
    return bool(used and used != embedder.EMBED_MODEL)


# ---------------------------------------------------------------- 벡터 (LanceDB)

_lance_lock = threading.RLock()


def _index_dir(agent_id: str) -> Path:
    return agent_dir(agent_id) / "index"


def _table(agent_id: str, create_with: list[dict] | None = None):
    import lancedb

    db = lancedb.connect(str(_index_dir(agent_id)))
    if "chunks" in db.table_names():
        return db.open_table("chunks")
    return db.create_table("chunks", data=create_with) if create_with else None


def add_vectors(agent_id: str, rows: list[dict]) -> None:
    """rows: [{"id": 조각 id, "doc_id": 문서 id, "vector": [float, ...]}]
    문서를 다시 처리할 때는 호출하는 쪽이 먼저 delete_vectors 로 이전 벡터를 지운다."""
    with _lance_lock:
        table = _table(agent_id)
        if table is None:
            _table(agent_id, create_with=rows)   # 표가 없으면 이 행들로 새로 만든다
        else:
            table.add(rows)


def delete_vectors(agent_id: str, doc_id: int) -> None:
    with _lance_lock:
        if not _index_dir(agent_id).exists():
            return
        table = _table(agent_id)
        if table is not None:
            table.delete(f"doc_id = {int(doc_id)}")


def vector_search(agent_id: str, qvec, k: int, doc_ids: set[int] | None = None) -> list[tuple[int, float]]:
    """[(조각 id, 코사인 유사도)] — 유사도가 높은 순."""
    with _lance_lock:
        if not _index_dir(agent_id).exists():
            return []
        table = _table(agent_id)
        if table is None or table.count_rows() == 0:
            return []
        if doc_ids is not None and not doc_ids:
            return []
        query = table.search(qvec).metric("cosine")
        if doc_ids is not None:
            query = query.where(f"doc_id IN ({','.join(str(int(x)) for x in sorted(doc_ids))})", prefilter=True)
        rows = query.limit(k).to_list()
    return [(int(r["id"]), 1.0 - float(r["_distance"])) for r in rows]


def drop_index(agent_id: str) -> None:
    with _lance_lock:
        remove_tree(_index_dir(agent_id))


def remove_tree(path: Path) -> None:
    """폴더를 지운다. 윈도우에서는 방금까지 쓰던 파일이 잠시 잠겨 있을 수 있어 몇 번 다시 시도한다."""
    for attempt in range(8):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.5)


def delete_document(agent_id: str, doc_id: int) -> bool:
    doc = get_document(agent_id, doc_id)
    if not doc:
        return False
    with closing(_connect(agent_id)) as conn, conn:
        purge_graph(conn, doc_id)
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    delete_vectors(agent_id, doc_id)
    (docs_dir(agent_id) / doc["stored_name"]).unlink(missing_ok=True)
    return True
