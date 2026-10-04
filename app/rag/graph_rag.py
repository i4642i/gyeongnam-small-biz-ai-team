"""지식 그래프를 질의응답에 연결한다 (그래프로 답변 근거를 보강).

일반 검색(벡터 + 키워드)은 질문과 글이 비슷한 조각을 찾는다. 그런데 "통신사 뭐가 있어?" 같은 목록형 질문은 답이 한 조각이 아니라
여러 조각에 흩어져 있고, 질문에 "Reuters"라는 이름은 없다. 그래프는 "글로벌 뉴스통신사 → Reuters, Bloomberg, Dow Jones"라는 분류를
알고 있으므로 이를 이용해 근거를 보강한다:

  1. 질문에 나온 개체를 그래프에서 찾는다: (가) 이름이 질문에 그대로 있거나 낱말이 이름 안에 있는 경우, (나) 이름과 설명을 임베딩한
     의미 벡터가 질문과 비슷한 경우("언론사 종류가 뭐야?" ↔ "글로벌 뉴스통신사"). (나)는 표현이 달라도 찾지만 틀릴 수 있어 기준을 둔다.
  2. 그 개체의 하위 항목·상위 분류·같은 분류의 다른 항목·이웃 개체를 모아 짧은 '그래프 정보'로 만든다 (프롬프트에 그대로 들어간다).
  3. 그 개체들이 나온 조각 중 일반 검색이 놓친 것을 몇 개 더 붙인다 (출처에 '그래프로 보강'이라고 표시한다).

그래프는 자동 추출이라 틀리거나 빠진 것이 있을 수 있으므로, 프롬프트에서 근거 자료([번호])를 우선하고 그래프 정보는 참고라고 밝힌다.
그래프가 없거나 질문에 나온 개체가 없으면 아무것도 더하지 않는다(일반 검색만 쓴다).
"""

import logging
import os
from contextlib import closing

import numpy as np

from app.rag import embedder, graph, search, store

log = logging.getLogger("agent_town.graph_rag")

MAX_ENTITIES = 3        # 질문 하나에서 다루는 개체 수
MAX_EXTRA_CHUNKS = 4    # 그래프로 더 붙이는 조각 수
MAX_LIST = 40           # 한 분류에서 나열하는 항목 수
MAX_INFO_CHARS = 2600   # 그래프 정보의 길이 상한
MAX_SEMANTIC = 2        # 의미로 더 찾는 개체 수 (이름으로 찾은 것과 별도)
# 의미 유사도 기준. 한국어 개체 20개(주식 출처 17개 + 다른 분야 3개)와 관련 질문 9개·무관 질문 8개로 bge-m3 를 실제로 재서 정했다:
#   이름+설명 임베딩: 관련 질문 1위 0.546~0.748(평균 0.653), 무관 질문 1위 최대 0.510  → 설명이 있는 개체는 0.55
#   이름만 임베딩:    관련 질문 1위 0.485~0.750, 무관 질문 1위 최대 0.544(고마워요→환불)  → 두 집단이 겹쳐서 설명 없는 개체는 더 엄격하게 0.65
# 표본이 작으므로(개체 20개) 실제 자료에서 엉뚱한 개체가 잡히면 올리고, 못 찾으면 내린다.
MIN_SEMANTIC_SIM = float(os.getenv("RAG_GRAPH_MIN_SIM", "0.55"))            # 설명이 있는 개체
MIN_SEMANTIC_SIM_NAME = float(os.getenv("RAG_GRAPH_MIN_SIM_NAME", "0.65"))  # 이름만 있는 개체(예전에 만든 그래프)


def enabled(agent_id: str) -> bool:
    """이 캐릭터가 답변에 그래프를 쓰는지(기본 켬). 그래프 탭에서 끌 수 있다."""
    return store.get_meta(agent_id, "graph_use") != "0"


def set_enabled(agent_id: str, value: bool) -> None:
    store.set_meta(agent_id, "graph_use", "1" if value else "0")


def _clean(text: str) -> str:
    """프롬프트 태그를 깨지 못하게 (개체 이름은 문서에서 모델이 뽑은 글이다)."""
    return text.replace("<", "＜").replace(">", "＞").replace("\n", " ")


def _load(agent_id: str):
    with closing(store._connect(agent_id)) as conn:
        ents = {r["id"]: dict(r) for r in conn.execute("SELECT id, name, norm, type, desc FROM entities")}
        if not ents:
            return None
        rels = [dict(r) for r in conn.execute("SELECT src, dst, label FROM relations GROUP BY src, dst, label")]
        chunks: dict[int, set[int]] = {}
        for r in conn.execute("SELECT entity_id, chunk_id FROM entity_chunks"):
            chunks.setdefault(r["entity_id"], set()).add(r["chunk_id"])
    return ents, rels, chunks


def _match(query: str, ents: dict, children: dict, chunks: dict) -> list[int]:
    """질문에 나온 개체 (점수순, 최대 MAX_ENTITIES)."""
    qn = graph._norm(query)
    tokens = {t for t in search.tokenize(query) if len(t) >= 2}
    scored = []
    for eid, e in ents.items():
        en = e["norm"]
        if len(en) < 2:
            continue
        if en in qn or en in tokens:
            score = 3                                   # 개체 이름이 질문에 그대로 있다
        elif any(t in en for t in tokens):
            score = 2                                   # 질문의 낱말이 개체 이름 안에 있다 ("통신사" ⊂ "글로벌 뉴스통신사")
        else:
            continue
        # 같은 점수면 하위 항목이 많은 분류를 앞에(목록형 질문), 그다음 자주 나온 개체를 앞에
        scored.append((score, len(children.get(eid, ())), len(chunks.get(eid, ())), eid))
    scored.sort(reverse=True)
    return [eid for *_, eid in scored[:MAX_ENTITIES]]


def _entity_text(e: dict) -> str:
    return f"{e['name']}. {e['desc']}" if e.get("desc") else e["name"]


def _vectors(agent_id: str, ents: dict, cap: int = 200) -> dict[int, np.ndarray]:
    """개체 id → 의미 벡터(이름 + 설명). 없거나 낡은(설명·모델이 바뀐) 것만 새로 만들어 저장한다(한 번에 최대 cap개)."""
    with closing(store._connect(agent_id)) as conn:
        rows = {r["entity_id"]: r for r in conn.execute("SELECT entity_id, vec, model, text FROM entity_vecs")}
    out: dict[int, np.ndarray] = {}
    todo: list[tuple[int, str]] = []
    for eid, e in ents.items():
        text, row = _entity_text(e), rows.get(eid)
        if row and row["model"] == embedder.EMBED_MODEL and row["text"] == text:
            out[eid] = np.frombuffer(row["vec"], dtype=np.float32)
        else:
            todo.append((eid, text))
    todo = todo[:cap]
    if todo:
        vecs = embedder.embed([t for _, t in todo])
        with closing(store._connect(agent_id)) as conn, conn:
            for (eid, text), v in zip(todo, vecs):
                conn.execute("INSERT OR REPLACE INTO entity_vecs (entity_id, vec, model, text) VALUES (?, ?, ?, ?)",
                             (eid, v.astype(np.float32).tobytes(), embedder.EMBED_MODEL, text))
                out[eid] = v
    return out


def semantic_scores(agent_id: str, query: str, ents: dict) -> list[tuple[int, float]]:
    """질문과 의미가 비슷한 개체 (유사도 큰 순). 임계값 이상만이 아니라 전부 돌려주고 고르는 것은 부르는 쪽이 한다(측정·시험용)."""
    vecs = _vectors(agent_id, ents)
    if not vecs:
        return []
    q = embedder.embed_query(query)
    return sorted(((eid, float(np.dot(v, q))) for eid, v in vecs.items()), key=lambda t: -t[1])


def augment(agent_id: str, query: str, hits: list[dict], k_extra: int = MAX_EXTRA_CHUNKS) -> dict:
    """{"extra": [조각...], "info": [줄...], "entities": [이름...]}. 보강할 것이 없으면 모두 빈 목록."""
    empty = {"extra": [], "info": [], "entities": [], "semantic": []}
    loaded = _load(agent_id)
    if not loaded:
        return empty
    ents, rels, chunk_of = loaded

    children: dict[int, list[int]] = {}
    parents: dict[int, list[int]] = {}
    related: dict[int, list[tuple[int, str]]] = {}
    for r in rels:
        if r["label"] == graph.CATEGORY:
            children.setdefault(r["src"], []).append(r["dst"])
            parents.setdefault(r["dst"], []).append(r["src"])
        else:
            related.setdefault(r["src"], []).append((r["dst"], r["label"]))
            related.setdefault(r["dst"], []).append((r["src"], r["label"]))

    matched = _match(query, ents, children, chunk_of)
    semantic: list[tuple[int, float]] = []
    try:   # 표현이 달라도 의미가 비슷한 개체. 실패해도(모델 오류 등) 이름으로 찾은 것만으로 계속한다
        semantic = [(eid, sim) for eid, sim in semantic_scores(agent_id, query, ents)[:8]
                    if sim >= (MIN_SEMANTIC_SIM if ents[eid].get("desc") else MIN_SEMANTIC_SIM_NAME) and eid not in matched][:MAX_SEMANTIC]
    except Exception:
        log.warning("개체 의미 검색 실패 (%s)", agent_id, exc_info=True)
    if not matched and not semantic:
        return empty
    sem_ids = {eid for eid, _ in semantic}
    matched = matched + [eid for eid, _ in semantic]

    name = lambda i: _clean(ents[i]["name"])
    by_name = lambda ids: sorted(dict.fromkeys(ids), key=lambda i: ents[i]["name"].casefold())
    info: list[str] = []
    focus: dict[int, int] = {}      # 조각을 가져올 개체 → 가중치
    desc_of = lambda i: _clean(ents[i].get("desc") or "")
    for eid in matched:
        focus[eid] = max(focus.get(eid, 0), 3 if eid not in sem_ids else 2)   # 의미로 찾은 개체는 이름으로 찾은 것보다 덜 믿는다
        if eid in sem_ids:
            info.append(f'질문과 의미가 비슷한 개체: "{name(eid)}"')
        if desc_of(eid):
            info.append(f'"{name(eid)}" 설명: {desc_of(eid)}')
        kids = by_name(children.get(eid, []))
        if kids:
            info.append(f'분류 "{name(eid)}" 아래 항목({len(kids)}개): ' + ", ".join(name(k) for k in kids[:MAX_LIST]) + (" …" if len(kids) > MAX_LIST else ""))
            for k in kids[:MAX_LIST]:
                focus[k] = max(focus.get(k, 0), 2)
                if desc_of(k):
                    info.append(f'  · {name(k)}: {desc_of(k)}')
                grand = by_name(children.get(k, []))
                if grand:
                    info.append(f'  - "{name(k)}" 아래: ' + ", ".join(name(g) for g in grand[:MAX_LIST]))
        for p in by_name(parents.get(eid, [])):
            sibs = [s for s in by_name(children.get(p, [])) if s != eid]
            info.append(f'"{name(eid)}"의 상위 분류: "{name(p)}"' + (f" (같은 분류의 다른 항목: {', '.join(name(s) for s in sibs[:MAX_LIST])})" if sibs else ""))
        for other, label in sorted(dict.fromkeys(related.get(eid, [])), key=lambda t: ents[t[0]]["name"].casefold())[:6]:
            info.append(f'관계: "{name(eid)}" — {_clean(label)} — "{name(other)}"')

    # 그래프 정보가 너무 길어지면 잘라 낸다
    text, total = [], 0
    for line in info:
        if total + len(line) > MAX_INFO_CHARS:
            text.append("… (길어서 생략)")
            break
        text.append(line)
        total += len(line)

    # 이 개체들이 나온 조각 중 일반 검색이 놓친 것
    have = {h["chunk_id"] for h in hits}
    score: dict[int, int] = {}
    for eid, w in focus.items():          # 질문에 나온 개체(3)와 그 직접 하위 항목(2)이 나온 조각만 후보다
        for cid in chunk_of.get(eid, ()):
            if cid not in have:
                score[cid] = score.get(cid, 0) + w
    ranked = sorted(score, key=lambda c: (-score[c], c))[: k_extra * 2]
    rows = store.chunk_rows(agent_id, ranked)          # 아직 처리 중인 문서의 조각은 여기서 빠진다
    extra = []
    for cid in ranked:
        if cid in rows and len(extra) < k_extra:
            r = rows[cid]
            extra.append({"chunk_id": cid, "doc_id": r["doc_id"], "filename": r["filename"], "page": r["page"], "text": r["text"],
                          "sim": None, "bm25": None, "score": score[cid], "via": "graph"})
    return {"extra": extra, "info": text, "entities": [ents[e]["name"] for e in matched],
            "semantic": [{"name": ents[e]["name"], "sim": round(sim, 3)} for e, sim in semantic]}
