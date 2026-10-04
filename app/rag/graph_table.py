"""지식 표 → 지식 그래프(2026-09-29, 뉴스 회사). 모델을 부르지 않는다.

파일 이름이 kg_ 로 시작하는 전문자료(MD)는 '지식 표'다. 표 한 줄 = 관계 하나:

    | 개체 | 개체 종류 | 관계 | 대상 | 대상 종류 | 설명 |

- 관계가 "포함"이면 분류(개체 = 상위 분류, 대상 = 하위 항목)로 저장한다(모델이 뽑는 그래프와 같은 규칙).
- 설명은 개체의 설명으로 붙는다. 대상·설명은 비워도 된다(대상이 비면 개체만 저장).
- 문서가 색인돼 조각으로 나뉜 뒤(표 머리는 조각마다 반복된다) 조각마다 행을 읽어 graph.save_chunk 로 넣는다.
  그래서 문서를 지우면 개체·관계도 함께 지워지고, 모델로 그래프 만들기를 해도 이 조각은 다시 부르지 않는다.
"""

from __future__ import annotations

import re
from contextlib import closing

from app.rag import graph, store

PREFIX = "kg_"
COLS = ["개체", "개체 종류", "관계", "대상", "대상 종류", "설명"]
_ROW = re.compile(r"^\|(.+)\|\s*$")


def is_table_doc(filename: str) -> bool:
    return (filename or "").lower().startswith(PREFIX)


def _cells(line: str) -> list[str] | None:
    m = _ROW.match(line.strip())
    if not m:
        return None
    return [c.strip() for c in m.group(1).split("|")]


def parse_rows(text: str) -> list[dict]:
    """조각 글에서 지식 표 행을 읽는다. 머리 행(개체·관계…)으로 칸 위치를 정하고, 구분 행(---)은 건너뛴다."""
    pos, rows = None, []
    for line in text.splitlines():
        cells = _cells(line)
        if cells is None:
            continue
        if "개체" in cells and "관계" in cells:
            pos = {c: cells.index(c) for c in COLS if c in cells}
            continue
        if pos is None or all(set(c) <= set("-: ") for c in cells):
            continue
        get = lambda k: cells[pos[k]] if k in pos and pos[k] < len(cells) else ""
        if get("개체"):
            rows.append({k: get(k) for k in COLS})
    return rows


def _typ(t: str) -> str:
    return t if t in graph.TYPES else "기타"


def rows_to_graph(rows: list[dict]) -> tuple[list[tuple[str, dict]], list[dict]]:
    ents: dict[str, dict] = {}
    rels: list[dict] = []

    def add(name: str, typ: str, desc: str = "") -> str | None:
        name = re.sub(r"\s+", " ", name).strip()[:graph.MAX_NAME]
        key = graph._norm(name)
        if len(name) < 2 or not key:
            return None
        e = ents.setdefault(key, {"name": name, "type": _typ(typ), "desc": ""})
        if e["type"] == "기타" and typ:
            e["type"] = _typ(typ)
        if desc and desc not in e["desc"]:
            e["desc"] = (f"{e['desc']} / {desc}" if e["desc"] else desc)[:graph.MAX_DESC_TOTAL]
        return key

    for r in rows:
        src = add(r["개체"], r["개체 종류"], r["설명"])
        if not src or not r["대상"]:
            continue
        dst = add(r["대상"], r["대상 종류"])
        label = re.sub(r"\s+", " ", r["관계"]).strip()[:30] or "관련"
        if dst and dst != src:
            rels.append({"src": src, "dst": dst, "label": label})
    return list(ents.items()), rels


def import_document(agent_id: str, doc_id: int) -> dict:
    """색인이 끝난 지식 표 문서 하나를 그래프에 넣는다. {"chunks", "entities", "relations"}."""
    with closing(store._connect(agent_id)) as conn:
        chunks = conn.execute("SELECT id, text FROM chunks WHERE doc_id = ? ORDER BY id", (doc_id,)).fetchall()
    n_e = n_r = 0
    for c in chunks:
        ents, rels = rows_to_graph(parse_rows(c["text"]))
        if graph.save_chunk(agent_id, c["id"], ents, rels):
            n_e += len(ents)
            n_r += len(rels)
    return {"chunks": len(chunks), "entities": n_e, "relations": n_r}


def import_all(agent_id: str) -> dict:
    """이 캐릭터의 지식 표 문서(색인 끝난 것) 전부를 다시 넣는다. 아직 색인 중인 표는 waiting 으로 알려 준다."""
    out = {"imported": [], "waiting": []}
    for d in store.list_documents(agent_id):
        if not is_table_doc(d["filename"]):
            continue
        if d["status"] != "ready":
            out["waiting"].append(d["filename"])
            continue
        out["imported"].append({"filename": d["filename"], **import_document(agent_id, d["id"])})
    return out
