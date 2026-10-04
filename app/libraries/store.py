"""컴퓨터 저장소. 회사가 함께 쓰는 문서 보관소 — 캐릭터(페르소나·모델)가 아니라 설비다.

data/libraries/l_xxxxxxxx/
    library.json  id, 이름, 설명, 만든 날짜, tools(설치된 도구 id — 직원의 도구처럼 도구 목록에서 고른다)
    jobs.json     작업 목록(app/libraries/collect.py): 어느 도구를 누가 어떤 인자로 실행했고 어떻게 됐는지
    agent.db      문서·조각 DB(app/rag/store.py 가 캐릭터 문서함과 같은 구조로 쓴다 — 파일 이름도 같게 둬서 RAG 코드를 그대로 쓴다)
    docs/         올린 원본 파일
    index/        벡터 색인

회사와의 연결은 office.json 의 "libraries": [컴퓨터 id, ...] 에 둔다(책상 배치와 별개).
"""

import json
import os
import re
import secrets
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.agents.store import AgentNotFound
from app.config import LIBRARIES_DIR

ID_RE = re.compile(r"^l_[0-9a-f]{8}$")
_lock = threading.RLock()


class LibraryNotFound(AgentNotFound):
    """RAG 코드가 AgentNotFound 를 잡아 '없는 주인'으로 처리하므로 그 하위 예외로 둔다."""


def is_library_id(owner_id: str) -> bool:
    return bool(ID_RE.match(owner_id or ""))


def library_dir(library_id: str) -> Path:
    """id를 검증한 뒤 폴더 경로를 돌려준다. id는 URL에서 오므로 경로 조작(../)을 여기서 막는다."""
    if not ID_RE.match(library_id or ""):
        raise LibraryNotFound(library_id)
    d = LIBRARIES_DIR / library_id
    if not (d / "library.json").is_file():
        raise LibraryNotFound(library_id)
    return d


def _write(d: Path, lib: dict) -> None:
    tmp = d / "library.json.tmp"
    tmp.write_text(json.dumps(lib, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, d / "library.json")


def create(name: str, description: str = "", tools: list[str] | None = None) -> dict:
    while True:
        library_id = "l_" + secrets.token_hex(4)
        d = LIBRARIES_DIR / library_id
        try:
            d.mkdir()
            break
        except FileExistsError:
            continue
    lib = {"id": library_id, "name": name, "description": description, "tools": list(tools if tools is not None else ["rag_index"]),   # 새 컴퓨터는 색인 도구를 기본으로 설치(빼면 보관만)
           "created_at": datetime.now(timezone.utc).isoformat()}
    _write(d, lib)
    return lib


def get(library_id: str) -> dict:
    return _normalize(json.loads((library_dir(library_id) / "library.json").read_text(encoding="utf-8")))


def _normalize(lib: dict) -> dict:
    """옛 형식(2026-09-27 오전, collects=["sec_filings"])을 설치 도구 목록으로 읽는다."""
    if "tools" not in lib:
        lib["tools"] = []
    lib.pop("collects", None)
    return lib


def list_all() -> list[dict]:
    libs = []
    for d in LIBRARIES_DIR.iterdir():
        if d.is_dir() and ID_RE.match(d.name) and (d / "library.json").is_file():
            try:
                libs.append(_normalize(json.loads((d / "library.json").read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                continue
    return sorted(libs, key=lambda x: x["created_at"], reverse=True)


def update(library_id: str, name: str, description: str, tools: list[str] | None = None) -> dict:
    with _lock:
        d = library_dir(library_id)
        lib = _normalize(json.loads((d / "library.json").read_text(encoding="utf-8")))
        lib["name"], lib["description"] = name, description
        if tools is not None:
            lib["tools"] = list(dict.fromkeys(tools))
        _write(d, lib)
        return lib


def delete(library_id: str) -> None:
    with _lock:
        shutil.rmtree(library_dir(library_id))
