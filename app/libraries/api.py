"""컴퓨터 API: 목록·상세·수집 작업·보관 자료·색인 장비. 문서 올리기·검색은 app/rag/api.py 의 /api/libraries/{id}/... 가 맡는다."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.libraries import collect, store
from app.offices import store as offices
from app.rag import search, store as rag_store, worker

router = APIRouter(prefix="/api", tags=["libraries"])


def _or_404(library_id: str) -> dict:
    try:
        return store.get(library_id)
    except store.LibraryNotFound:
        raise HTTPException(404, "컴퓨터를 찾을 수 없습니다")


def _state(docs: list[dict], jobs: list[dict]) -> str:
    """그림의 모니터 색: busy(받는 중·색인 중) > failed(실패한 문서가 있거나 마지막 작업이 실패) > ready > empty."""
    if any(d["status"] in rag_store.PROCESSING for d in docs) or any(j["status"] in collect.ACTIVE for j in jobs):
        return "busy"
    if any(d["status"] == "failed" for d in docs) or (jobs and jobs[-1]["status"] == "failed"):
        return "failed"
    return "ready" if any(d["status"] == "ready" for d in docs) else "empty"


def _with_stats(lib: dict, all_offices: list[dict]) -> dict:
    docs = rag_store.list_documents(lib["id"])
    jobs = collect.load_jobs(lib["id"])
    return {**lib,
            "state": _state(docs, jobs),
            "active_jobs": sum(1 for j in jobs if j["status"] in collect.ACTIVE),
            "documents": len(docs),
            "ready": sum(1 for d in docs if d["status"] == "ready"),
            "processing": sum(1 for d in docs if d["status"] in rag_store.PROCESSING),
            "tickers": sorted({d["ticker"] for d in docs if d.get("ticker")}),
            "offices": [{"id": o["id"], "name": o["name"]} for o in all_offices if lib["id"] in (o.get("libraries") or [])]}


@router.get("/libraries")
def list_libraries():
    all_offices = offices.list_all()
    return {"libraries": [_with_stats(x, all_offices) for x in store.list_all()]}


@router.get("/libraries/{library_id}")
def get_library(library_id: str):
    return _with_stats(_or_404(library_id), offices.list_all())


class JobIn(BaseModel):
    tool: str = Field(min_length=1, max_length=40)
    args: list[dict] = Field(min_length=1, max_length=30)   # 인자 묶음마다 작업 하나(예: 종목 여러 개)


def _tool_view(tid: str) -> dict:
    from app.tools import registry
    t = registry.get(tid)
    if t is None:
        return {"id": tid, "label": tid, "missing": True, "params": []}
    return {"id": t.id, "label": t.label or t.id, "description": t.description, "usage": t.usage, "format": t.format,
            "params": [{"name": p.name, "type": p.type, "description": p.description, "required": p.required, "enum": p.enum} for p in t.params]}


@router.get("/libraries/{library_id}/jobs")
def list_jobs(library_id: str):
    lib = _or_404(library_id)
    jobs = [collect.live_status(library_id, j) for j in reversed(collect.load_jobs(library_id))][:100]
    return {"jobs": jobs, "tools": [_tool_view(t) for t in lib["tools"]]}


@router.post("/libraries/{library_id}/jobs", status_code=201)
def request_jobs(library_id: str, body: JobIn):
    """사람이 컴퓨터 화면에서 설치된 도구를 실행하라고 요청한다(흐름의 '시작 전 준비'와 같은 작업)."""
    lib = _or_404(library_id)
    if body.tool == "rag_index":
        from app.libraries import indexing
        if "rag_index" not in lib["tools"]:
            raise HTTPException(409, f"'{lib['name']}'에 설치되지 않은 도구입니다: rag_index")
        return {"jobs": [indexing.run(library_id, str(a.get("target") or "새 문서만"), "사람(컴퓨터 화면)") for a in body.args[:1]]}
    try:
        jobs = collect.request(library_id, body.tool, body.args, requested_by="사람(컴퓨터 화면)")
    except collect.CollectError as e:
        raise HTTPException(409, str(e))
    return {"jobs": jobs}


# ---------------------------------------------------------------- 보관 자료 상태(관리자 화면)

def _doc_or_404(library_id: str, doc_id: int) -> dict:
    _or_404(library_id)
    doc = rag_store.get_document(library_id, doc_id)
    if not doc:
        raise HTTPException(404, "문서를 찾을 수 없습니다")
    return doc


@router.get("/libraries/{library_id}/inventory")
def inventory(library_id: str):
    """이 컴퓨터에 무엇이 들어 있고 쓸 수 있는 상태인지: 요약 수치 + 문서마다 종목·서식·기간·게재일·접수번호·색인 상태·받아 온 작업."""
    import json as _json
    _or_404(library_id)
    origin = {}   # 문서 id → 그 문서를 받아 온(처음 등록한) 작업
    for j in collect.load_jobs(library_id):
        for d in j.get("documents", []):
            if not d.get("reused") and d.get("doc_id") not in origin:
                origin[d["doc_id"]] = {"job": j["id"], "tool": j["tool"], "requested_by": j["requested_by"], "at": j["created_at"]}
    from app.libraries import indexing
    pos = indexing.queue_position(library_id)
    rows = []
    for d in rag_store.list_documents(library_id):
        try:
            ev = _json.loads(d.get("date_evidence") or "{}")
        except ValueError:
            ev = {}
        rows.append({"id": d["id"], "filename": d["filename"], "size": d["size"], "ticker": d.get("ticker"), "doc_type": d.get("doc_type"),
                     "period": d.get("period"), "published_at": d.get("published_at"), "date_status": d.get("date_status"),
                     "accession": ev.get("accession"), "source_url": ev.get("filing_url") or ev.get("document_url") or ev.get("source_url"),
                     "status": d["status"], "progress_done": d["progress_done"], "progress_total": d["progress_total"], "chunks": d["chunks"],
                     "error": d.get("error"), "search_excluded": bool(d.get("search_excluded")), "excluded_reason": d.get("excluded_reason"),
                     "created_at": d["created_at"], "origin": origin.get(d["id"]), "queue_pos": pos.get(d["id"])})
    summary = {"documents": len(rows), "tickers": len({r["ticker"] for r in rows if r["ticker"]}),
               "ready": sum(1 for r in rows if r["status"] == "ready" and not r["search_excluded"]),
               "processing": sum(1 for r in rows if r["status"] in rag_store.PROCESSING),
               "failed": sum(1 for r in rows if r["status"] == "failed"),
               "excluded": sum(1 for r in rows if r["search_excluded"]),
               "no_date": sum(1 for r in rows if r["status"] == "ready" and not r["published_at"]),
               "stored": sum(1 for r in rows if r["status"] == indexing.STORED)}
    from app.rag import embedder, worker
    index = {"installed": indexing.installed(library_id), "model": rag_store.get_meta(library_id, "embed_model") or embedder.EMBED_MODEL,
             "chunks": sum(r["chunks"] for r in rows if r["status"] == "ready"), "stale": rag_store.is_stale(library_id),
             "queue_total": len(worker.snapshot()), "queue_mine": len(pos),
             "device": {"local": embedder.device_config()["use_local"], "saved": embedder.device_config()["saved_url"] or None, "remote": embedder.device_config()["url"] or None, "state": embedder.remote["state"], "message": embedder.remote["message"]}}
    return {"summary": summary, "documents": rows, "index": index}


@router.get("/libraries/{library_id}/documents/{doc_id}/chunks")
def document_chunks(library_id: str, doc_id: int, start: int = 0, size: int = 50):
    """색인 결과 보기: 이 문서가 어떤 조각으로 나뉘었고 조각마다 벡터가 만들어졌는지(벡터 길이)."""
    from contextlib import closing
    doc = _doc_or_404(library_id, doc_id)
    size = max(10, min(size, 200))
    start = max(0, start)
    with closing(rag_store._connect(library_id)) as conn:
        total = conn.execute("SELECT COUNT(*) FROM chunks WHERE doc_id = ?", (doc_id,)).fetchone()[0]
        rows = [dict(r) for r in conn.execute("SELECT id, ord, page, text FROM chunks WHERE doc_id = ? ORDER BY ord LIMIT ? OFFSET ?",
                                              (doc_id, size, start))]
    dims, have = None, set()
    try:
        table = rag_store._table(library_id)
        if table is not None and rows:
            ids = ",".join(str(r["id"]) for r in rows)
            got = table.search().where(f"id IN ({ids})").select(["id", "vector"]).limit(len(rows)).to_list()
            have = {g["id"] for g in got}
            dims = len(got[0]["vector"]) if got else None
    except Exception:
        dims = None   # 벡터 표를 못 읽어도 조각 글은 보여 준다
    return {"filename": doc["filename"], "status": doc["status"], "total": total, "start": start, "vector_dims": dims,
            "chunks": [{"n": r["ord"] + 1, "page": r["page"], "chars": len(r["text"]), "text": r["text"], "vector": r["id"] in have} for r in rows]}


@router.get("/libraries/{library_id}/documents/{doc_id}/text")
def document_text(library_id: str, doc_id: int, start: int = 0, size: int = 20000):
    """문서 원문 읽기(관리자 확인용). .md·.txt 는 올린 파일 그대로, 그 밖(PDF)은 뽑아 둔 조각 글을 이어서 보여 준다."""
    doc = _doc_or_404(library_id, doc_id)
    size = max(1000, min(size, 50000))
    start = max(0, start)
    path = rag_store.docs_dir(library_id) / doc["stored_name"]
    if doc["ext"] in (".md", ".txt", ".markdown") and path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
    else:
        from contextlib import closing
        with closing(rag_store._connect(library_id)) as conn:
            text = "\n\n".join(r[0] for r in conn.execute("SELECT text FROM chunks WHERE doc_id = ? ORDER BY ord", (doc_id,)))
    return {"filename": doc["filename"], "total": len(text), "start": start, "text": text[start:start + size]}


class ExcludeIn(BaseModel):
    reason: str = Field(min_length=2, max_length=300)


@router.post("/libraries/{library_id}/documents/{doc_id}/exclude")
def exclude_document(library_id: str, doc_id: int, body: ExcludeIn):
    _doc_or_404(library_id, doc_id)
    rag_store.exclude_from_search(library_id, doc_id, body.reason.strip())
    search.invalidate(library_id)
    return {"ok": True}


@router.post("/libraries/{library_id}/documents/{doc_id}/include")
def include_document(library_id: str, doc_id: int):
    _doc_or_404(library_id, doc_id)
    rag_store.include_in_search(library_id, doc_id)
    search.invalidate(library_id)
    return {"ok": True}


# ---------------------------------------------------------------- 파일(컴퓨터 폴더 안 모든 파일)

_TEXT_EXT = {".md", ".txt", ".json", ".markdown"}


def _file_role(rel: str) -> str:
    top = rel.split("/")[0]
    return {"docs": "원문(받거나 올린 문서)", "index": "색인(조각별 의미 벡터 — LanceDB)", "agent.db": "문서 DB(문서 목록·조각 글·색인 상태)",
            "jobs.json": "작업 기록", "library.json": "컴퓨터 설정(이름·설명·설치된 도구)"}.get(top, "기타")


@router.get("/libraries/{library_id}/files")
def list_files(library_id: str):
    """이 컴퓨터가 관리하는 파일 전부(폴더 안 모든 파일). 원문 파일은 어느 문서인지(원래 이름)도 붙인다."""
    from datetime import datetime, timezone
    _or_404(library_id)
    root = store.library_dir(library_id)
    by_stored = {d["stored_name"]: d for d in rag_store.list_documents(library_id)}
    files = []
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.name.endswith(".tmp"):
            continue
        rel = f.relative_to(root).as_posix()
        st = f.stat()
        doc = by_stored.get(f.name) if rel.startswith("docs/") else None
        files.append({"path": rel, "size": st.st_size, "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                      "role": _file_role(rel), "text": f.suffix.lower() in _TEXT_EXT,
                      "doc": {"id": doc["id"], "filename": doc["filename"], "ticker": doc.get("ticker"), "status": doc["status"]} if doc else None})
    return {"root": str(root), "files": files, "total_size": sum(x["size"] for x in files)}


@router.get("/libraries/{library_id}/files/content")
def file_content(library_id: str, path: str, start: int = 0, size: int = 20000):
    """파일 하나 열기(글 파일만). 경로는 이 컴퓨터 폴더 안이어야 한다."""
    _or_404(library_id)
    root = store.library_dir(library_id).resolve()
    target = (root / path).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(404, "이 컴퓨터 안에 그런 파일이 없습니다")
    if target.suffix.lower() not in _TEXT_EXT:
        raise HTTPException(415, "글 파일이 아니라 내용을 보여 줄 수 없습니다(크기·수정 시각만 볼 수 있습니다).")
    text = target.read_text(encoding="utf-8", errors="replace")
    size = max(1000, min(size, 50000))
    start = max(0, start)
    return {"path": path, "total": len(text), "start": start, "text": text[start:start + size]}


# ---------------------------------------------------------------- 색인 장비(문서 색인 도구의 설정 — 모든 컴퓨터가 함께 쓴다)

class DeviceIn(BaseModel):
    url: str = Field(default="", max_length=200)          # 비우면 이 PC 로 계산
    token: str | None = Field(default=None, max_length=200)   # None = 지금 토큰 그대로
    rerank: bool | None = None   # 검색 순위 매기기도 장비로(None = 지금 값 그대로)


def _device_view() -> dict:
    from app.rag import embedder
    cfg = embedder.device_config()
    return {"url": cfg["url"], "token_set": bool(cfg["token"]), "state": embedder.remote["state"] if cfg["url"] else "off",
            "message": embedder.remote["message"], "model": embedder.EMBED_MODEL, "rerank": cfg["rerank"]}


def _check_url(url: str) -> str:
    import re
    url = url.strip().rstrip("/")
    if url and not re.match(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?$", url):
        raise HTTPException(422, "주소는 http://IP:포트 형식이어야 합니다(예: http://192.168.0.15:8790).")
    return url


@router.get("/index-device")
def get_device():
    return _device_view()


class DeviceModeIn(BaseModel):
    local: bool   # True = 이 PC 로 계산, False = 저장된 젯슨 주소로 계산


@router.put("/index-device/mode")
def put_device_mode(body: DeviceModeIn):
    from app.rag import embedder
    if not body.local and not embedder.device_config()["saved_url"]:
        raise HTTPException(422, "저장된 젯슨 주소가 없습니다. 먼저 색인 장비 주소를 등록해야 합니다.")
    embedder.set_use_local(body.local)
    return _device_view()


@router.put("/index-device")
def put_device(body: DeviceIn):
    from app.rag import embedder
    embedder.save_device_config(_check_url(body.url), body.token, body.rerank)
    return _device_view()


@router.post("/index-device/test")
def test_device(body: DeviceIn):
    """저장 전에도 시험할 수 있다: 칸에 적은 주소·토큰(비워 두면 저장된 값)으로 장비에 물어본다."""
    from app.rag import embedder
    cfg = embedder.device_config()
    url = _check_url(body.url) or cfg["saved_url"]   # 이 PC 모드에서도 젯슨으로 바꾸기 전에 저장된 주소를 시험할 수 있게
    token = body.token if body.token else cfg["token"]
    if not url:
        raise HTTPException(422, "시험할 주소가 없습니다.")
    try:
        return embedder.device_health(url, token)
    except Exception as e:
        msg = str(e)
        if "401" in msg:
            msg = "토큰이 맞지 않습니다."
        elif "timed out" in msg or "refused" in msg or "unreachable" in msg.lower():
            msg = f"장비에 연결하지 못했습니다({msg}). 젯슨이 켜져 있고 색인 서버가 떠 있는지 확인하세요."
        return {"ok": False, "health": None, "problem": msg}
