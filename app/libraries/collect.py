"""회사 컴퓨터의 작업: 컴퓨터에 설치된 도구를 실행하고, 그 결과를 문서로 보관한다. 판단이 없는 일이라 모델을 부르지 않는다.

- 요청(request): 사람(컴퓨터 화면), 흐름(부서 시작 전 준비)이 "어느 도구를 어떤 인자로" 실행하라고 넘긴다.
  작업 목록은 컴퓨터 폴더의 jobs.json 에 남아 화면에서 누가·언제·무엇을 요청했고 어떻게 됐는지 볼 수 있다.
- 처리(한 줄로 하나씩): 뉴스 모으기(naver_news)는 기사 한 건을 문서 한 건으로(이미 가진 기사는 다시 받지 않음),
  그 밖의 도구는 도구 결과 글 하나를 문서로 등록하고 색인 줄에 넣는다(app/rag/worker).
- 준비 확인(readiness): 작업이 끝났고, 그 작업이 만든(또는 이미 있던) 문서의 색인이 모두 끝났으면 준비됨.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import secrets
import threading
import time
from datetime import datetime, timezone

from app.libraries import store
from app.rag import store as rag_store

log = logging.getLogger("agent_town.collect")

_lock = threading.RLock()
_queue: queue.Queue = queue.Queue()
_thread: threading.Thread | None = None

ACTIVE = ("queued", "running")


class CollectError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jobs_path(library_id: str):
    return store.library_dir(library_id) / "jobs.json"


def load_jobs(library_id: str) -> list[dict]:
    p = _jobs_path(library_id)
    if not p.is_file():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("jobs", [])
    except (OSError, ValueError):
        log.warning("작업 목록을 읽지 못했습니다: %s", p, exc_info=True)
        return []


def _save_jobs(library_id: str, jobs: list[dict]) -> None:
    p = _jobs_path(library_id)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"jobs": jobs[-500:]}, ensure_ascii=False, indent=1), encoding="utf-8")
    # Windows 는 다른 쪽(흐름의 준비 확인·화면)이 jobs.json 을 읽는 순간에 바꿔 치우면 PermissionError(WinError 32)를 낸다
    # (2026-09-28 은행 98개 받기 중 1건 실제 발생) — 잠깐 기다렸다 다시 시도한다.
    for attempt in range(10):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.05 * (attempt + 1))


def _update_job(library_id: str, job_id: str, **fields) -> None:
    with _lock:
        jobs = load_jobs(library_id)
        for j in jobs:
            if j["id"] == job_id:
                j.update(fields, updated_at=_now())
        _save_jobs(library_id, jobs)


def request(library_id: str, tool_id: str, args_list: list[dict], requested_by: str, reason: str = "") -> list[dict]:
    """인자 묶음마다 작업 하나를 줄 세운다. 같은 도구·같은 인자의 작업이 이미 줄에 있으면 그 작업을 돌려준다(새로 안 만든다).
    돌려주는 목록은 args_list 순서와 같다(흐름이 항목마다 어느 작업인지 알 수 있게)."""
    from app.tools import registry
    lib = store.get(library_id)
    if tool_id not in lib.get("tools", []):
        raise CollectError(f"'{lib['name']}'에 설치되지 않은 도구입니다: {tool_id}")
    if registry.get(tool_id) is None:
        raise CollectError(f"도구 목록에 없는 도구입니다: {tool_id}")
    out, made = [], []
    with _lock:
        jobs = load_jobs(library_id)
        for args in args_list:
            args = {k: v for k, v in (args or {}).items() if v not in (None, "")}
            same = next((j for j in jobs if j["tool"] == tool_id and j["args"] == args and j["status"] in ACTIVE), None)
            if same:
                out.append(same)
                continue
            job = {"id": "j_" + secrets.token_hex(4), "tool": tool_id, "args": args, "requested_by": requested_by, "reason": reason,
                   "status": "queued", "notes": [], "documents": [], "created_at": _now(), "updated_at": _now()}
            jobs.append(job)
            made.append(job)
            out.append(job)
        _save_jobs(library_id, jobs)
    for job in made:
        _enqueue(library_id, job["id"])
    return out


def record(library_id: str, tool_id: str, args: dict, requested_by: str, reason: str = "", documents: list | None = None,
           status: str = "done", notes: list | None = None) -> dict:
    """줄 세워 실행하지 않고 작업 기록만 남긴다(색인처럼 다른 일꾼이 처리하는 일). 상태는 live_status 가 문서 상태로 계산한다."""
    job = {"id": "j_" + secrets.token_hex(4), "tool": tool_id, "args": args, "requested_by": requested_by, "reason": reason,
           "status": status, "notes": notes or [], "documents": documents or [], "created_at": _now(), "updated_at": _now()}
    with _lock:
        jobs = load_jobs(library_id)
        jobs.append(job)
        _save_jobs(library_id, jobs)
    return job


def live_status(library_id: str, job: dict) -> dict:
    """색인 작업은 기록 뒤 색인 일꾼이 처리하므로, 화면에 보일 상태·진행을 문서 상태로 계산한다."""
    if job["tool"] != "rag_index" or job["status"] in ACTIVE or not job.get("documents"):
        return job
    states = [(rag_store.get_document(library_id, d["doc_id"]) or {}) for d in job["documents"]]
    busy = [s for s in states if s.get("status") in rag_store.PROCESSING]
    failed = [s for s in states if s.get("status") == "failed"]
    done = sum(1 for s in states if s.get("status") == "ready")
    status = "running" if busy else "failed" if failed else "done"
    note = f"색인 {done}/{len(states)}건 끝남" + (f" · 실패 {len(failed)}" if failed else "")
    return {**job, "status": status, "notes": [note] + [n for n in job.get("notes", []) if not n.startswith("색인 ")]}


def _enqueue(library_id: str, job_id: str) -> None:
    global _thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="computer-jobs", daemon=True)
            _thread.start()
    _queue.put((library_id, job_id))


def _loop() -> None:
    while True:
        library_id, job_id = _queue.get()
        try:
            _process(library_id, job_id)
        except store.LibraryNotFound:
            pass
        except Exception as e:
            log.exception("컴퓨터 작업 실패 (%s, %s)", library_id, job_id)
            try:
                _update_job(library_id, job_id, status="failed", notes=[f"처리 중 오류: {type(e).__name__}: {e}"])
            except Exception:
                pass


def known_news_urls(library_id: str) -> set[str]:
    """이 컴퓨터가 가진 기사의 정규화 주소(뉴스 모으기 작업 기록 + 문서 정보표의 원문) — 같은 기사를 다시 받지 않게."""
    from app.tools.newscollect import _norm_url
    out = set()
    docs = {d["id"]: d for d in rag_store.list_documents(library_id)}
    for j in load_jobs(library_id):
        for x in j.get("documents", []):
            if x.get("url") and x.get("doc_id") in docs:
                out.add(_norm_url(x["url"]))
    for d in docs.values():
        try:
            u = json.loads(d.get("date_evidence") or "{}").get("source_url")
        except ValueError:
            u = None
        if u:
            out.add(_norm_url(u))
    return out


def _process(library_id: str, job_id: str) -> None:
    from app.offices import rag_prep
    from app.tools import registry

    job = next((j for j in load_jobs(library_id) if j["id"] == job_id), None)
    if job is None or job["status"] not in ACTIVE:
        return
    _update_job(library_id, job_id, status="running")
    tool = registry.get(job["tool"])
    if tool is None:
        _update_job(library_id, job_id, status="failed", notes=[f"도구 목록에 없는 도구입니다: {job['tool']}"])
        return

    if tool.format == "rag_index":
        from app.libraries import indexing
        docs = indexing.select(library_id, str(job["args"].get("target") or "새 문서만"))
        indexing.enqueue_docs(library_id, [d["id"] for d in docs])
        _update_job(library_id, job_id, status="done", notes=[] if docs else ["색인할 문서가 없습니다."],
                    documents=[{"doc_id": d["id"], "filename": d["filename"]} for d in docs])
        return

    if tool.format in ("news_collect", "naver_news"):
        mod = __import__(f"app.tools.{'newscollect' if tool.format == 'news_collect' else 'naver_news'}", fromlist=["collect"])
        res = mod.collect(job["args"], known_news_urls(library_id))
        documents = []
        for d in res["documents"]:
            doc_id = rag_prep._register_document(library_id, d["filename"], d["content"], auto_index=False)
            documents.append({"doc_id": doc_id, "filename": d["filename"], "url": d["url"], "published": d["published"]})
        from app.libraries import indexing
        indexing.index_batch(library_id, [x["doc_id"] for x in documents], "자동(뉴스 모으기)")
        _update_job(library_id, job_id, status="done" if res["ok"] else "failed", notes=res["notes"], documents=documents)
        return

    from app.tools import runner
    result = runner.execute(tool, job["args"], runner.Context(agent_id=library_id, mode="task"))
    if not result.ok:
        _update_job(library_id, job_id, status="failed", notes=[result.entry.get("summary") or "도구 실행 실패",
                                                                  (result.entry.get("text") or "")[:300]])
        return
    stamp = _now()
    header = ("| 항목 | 값 |\n|---|---|\n"
              f"| 도구 | {tool.label or tool.id} ({tool.id}) |\n| 인자 | {json.dumps(job['args'], ensure_ascii=False)} |\n"
              f"| 받은 시각 | {stamp} |\n\n---\n\n")
    filename = f"{tool.id}_{stamp[:19].replace(':', '').replace('-', '')}.md"
    lines = result.text.splitlines()   # 결과 글 전체(대화 기록용 요약 text 는 6000자로 잘려 있어 쓰지 않는다) — 감싼 태그 두 줄·한 줄을 벗긴다
    body = "\n".join(lines[2:-1]) if len(lines) >= 3 and lines[0].startswith("<도구결과") else result.text
    doc_id = rag_prep._register_document(library_id, filename, header + body)
    _update_job(library_id, job_id, status="done", notes=[result.entry.get("summary") or "결과를 받았습니다"],
                documents=[{"doc_id": doc_id, "filename": filename}])


def readiness(library_id: str, job_ids: list[str], require_index: bool = True) -> dict[str, dict]:
    """작업마다 {"state": ready|waiting|failed, "note"}. ready = 작업이 끝났고 그 문서들의 색인이 모두 끝남.
    require_index=False(흐름 준비의 '받기까지만', 2026-09-28): 원문을 받아 보관만 했어도 준비됨 — 원문을 직접 읽는 도구용(동종 비교 CET1)."""
    jobs = {j["id"]: j for j in load_jobs(library_id)}
    out = {}
    for jid in job_ids:
        j = jobs.get(jid)
        if j is None:
            out[jid] = {"state": "failed", "note": "작업 기록이 없습니다"}
            continue
        if j["status"] in ACTIVE:
            out[jid] = {"state": "waiting", "note": "받는 중" if j["status"] == "running" else "대기"}
            continue
        if j["status"] == "failed":
            out[jid] = {"state": "failed", "note": "; ".join(j.get("notes") or []) or "실패"}
            continue
        states = [(rag_store.get_document(library_id, d["doc_id"]) or {}).get("status") for d in j.get("documents", [])]
        if not states:
            out[jid] = {"state": "failed", "note": "; ".join(j.get("notes") or []) or "받은 문서가 없습니다"}
        elif any(s in rag_store.PROCESSING for s in states):
            out[jid] = {"state": "waiting", "note": f"색인 중 {sum(1 for s in states if s in rag_store.PROCESSING)}건"}
        elif not require_index and all(s in ("stored", "ready") for s in states):
            out[jid] = {"state": "ready", "note": ""}
        elif any(s == "stored" for s in states):
            out[jid] = {"state": "failed", "note": "받은 문서를 보관만 했습니다 — 이 컴퓨터에 '문서 색인' 도구가 없어 색인하지 않았습니다"}
        elif any(s != "ready" for s in states):
            out[jid] = {"state": "failed", "note": "문서 처리 실패 또는 삭제됨"}
        else:
            out[jid] = {"state": "ready", "note": ""}
    return out


def recover() -> None:
    """서버를 켤 때: 끝나지 않은 작업을 다시 줄 세운다."""
    for lib in store.list_all():
        for j in load_jobs(lib["id"]):
            if j["status"] in ACTIVE:
                _update_job(lib["id"], j["id"], status="queued")
                _enqueue(lib["id"], j["id"])
