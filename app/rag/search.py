"""하이브리드 검색: 벡터(의미) 검색 + 키워드(BM25) 검색을 RRF 로 합친다.

- 벡터 검색은 "말이 달라도 뜻이 같은" 조각을 찾고, 키워드 검색은 고유명사·숫자·조항 번호처럼 정확히 같은 낱말을 찾는다.
  둘의 순위를 RRF(각 순위의 1/(60+순위)를 더함)로 합치면 서로의 약점이 메워진다.
- 한국어는 조사·어미가 붙어서 그냥 공백으로 자르면 키워드가 안 맞는다("환불을" ≠ "환불"). kiwipiepy 형태소 분석기로
  명사·동사 어근·숫자·영단어만 뽑아서 비교한다.
- 관련 없는 조각을 끼워 넣지 않도록 기준을 둔다: 벡터 유사도가 MIN_VECTOR_SIM 이상이거나, 키워드 검색에서 강하게 맞아야 한다.
  ("안녕하세요" 같은 인사에는 자료를 붙이지 않는다.)
"""

import logging
import os
import re
import threading
from collections import defaultdict

from rank_bm25 import BM25Okapi

from app.rag import embedder, store

log = logging.getLogger("agent_town.search")

TOP_K = 6            # 대화에 붙일 조각 수
CANDIDATES = 20      # 벡터·키워드 각각에서 먼저 뽑는 후보 수
RRF_K = 60
# 관련 없는 조각을 끼워 넣지 않는 기준. 한국어 시험 문서(환불 규정·근로계약·표가 있는 PDF·레시피)로 실제 점수를 재서 정했다:
#   관련 질문의 정답 조각   유사도 0.56~0.74 (조항 번호를 그대로 묻는 "제8조 분쟁 해결"만 0.39, 대신 키워드 점수 5.86)
#   관련 없는 질문의 1위    유사도 최대 0.49("비트코인 가격 전망"), 인사말 0.40, 키워드 점수 최대 3.44(단어 "가격"이 우연히 일치)
# 관련 조각을 놓치는 것이 잘못 끼워 넣는 것보다 나쁘다고 보고(놓치면 사용자가 모르는 채 자료 없이 답한다. 잘못 끼워 넣으면
# 모델이 "자료에 없다"고 말한다) 인사말은 걸러내되 조금 느슨하게 잡았다. 환경변수로 조정할 수 있다.
MIN_VECTOR_SIM = float(os.getenv("RAG_MIN_VECTOR_SIM", "0.45"))   # 벡터 유사도가 이 이상이면 관련 있다고 본다
MIN_BM25_SCORE = float(os.getenv("RAG_MIN_BM25", "5.0"))         # 유사도가 낮아도 키워드가 이 점수 이상으로 강하게 맞으면 포함 (고유명사·숫자·조항 번호)
MIN_SIM_WITH_KEYWORD = 0.30                                      # 다만 유사도를 아는 조각은 이 밑이면 키워드가 맞아도 버린다
# 다시 추리기(리랭커, 2026-09-22 추가): bge-m3 와 같은 계열의 cross-encoder — 후보 20개를 질문과 조각을 같이 넣어
# 다시 점수 매겨 상위 k 개만 남긴다. RAG_MIN_RELEVANCE 밑은 버린다("없다"고 답해야 하는 질문에서 무관한 조각이
# 그럴듯하게 끼어드는 것을 줄인다 — 값은 실측해서 정함, RAG 개선 6번 시험표 참고).
RERANK_MODEL = os.getenv("RERANK_MODEL") or "BAAI/bge-reranker-v2-m3"
USE_RERANKER = os.getenv("RAG_USE_RERANKER", "1") != "0"
# ⚠ 임시값(provisional, 2026-09-23 확인): 표본 5개로 정한 값이라 근거가 약하다(관련 쌍 0.59~0.73,
# 무관 쌍 0.50~0.52, 시그모이드 적용). 2026-09-23 재측정에서도 "자료 없음" 질문 6개 중 0개만
# 빈 결과를 내 — 이 문턱으로는 오탐(관련 없는데 걸리는 것)을 못 거르고 있다. 표본을 늘리기 전엔
# 이 값 자체를 더 조정하지 않기로 함(대표님 판단, 2026-09-23) — 바꾸려면 먼저 표본을 늘릴 것.
MIN_RELEVANCE = float(os.getenv("RAG_MIN_RELEVANCE", "0.55"))
_reranker = None
_reranker_lock = threading.Lock()


def _get_reranker():
    global _reranker
    with _reranker_lock:
        if _reranker is None:
            from sentence_transformers import CrossEncoder

            _reranker = CrossEncoder(RERANK_MODEL, cache_folder=str(embedder.MODELS_DIR), device="cpu", max_length=512, activation_fn=None)
        return _reranker


def _sig(x: float) -> float:
    import math
    return 1.0 / (1.0 + math.exp(-x))


def _remote_rerank(query: str, texts: list[str], dev: dict) -> list[float]:
    """젯슨 /rerank 에서 원점수(로짓)를 받는다. 실패하면 3·10·30초 간격으로 다시 시도하고, 끝내 안 되면 오류 —
    PC CPU 로 넘어가지 않는다(색인과 같은 원칙, 2026-09-29)."""
    import json
    import time
    import urllib.request
    last = ""
    for wait in (0, 3, 10, 30):
        if wait:
            time.sleep(wait)
        try:
            out: list[float] = []
            for i in range(0, len(texts), 64):
                req = urllib.request.Request(f"{dev['url']}/rerank", data=json.dumps({"query": query, "texts": texts[i:i + 64]}).encode("utf-8"),
                                             headers={"Content-Type": "application/json", "X-Embed-Token": dev["token"]})
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.loads(r.read())
                if data.get("model") != RERANK_MODEL:
                    raise ValueError(f"장비의 리랭커 모델이 다릅니다: {data.get('model')}")
                out.extend(float(x) for x in data["logits"])
            return out
        except Exception as e:
            last = f"{type(e).__name__}: {e}"[:200]
            log.warning("원격 리랭커 실패 — 다시 시도합니다(PC CPU 로는 계산하지 않음): %s", last)
    raise RuntimeError(f"색인 장비(젯슨 {dev['url']})의 리랭커가 응답하지 않습니다 — PC CPU 로는 계산하지 않습니다(마지막 오류: {last}).")


def rerank(query: str, cids: list[int], rows: dict[int, dict]) -> list[tuple[int, float]]:
    """[(조각 id, 관련도 점수 0~1)] 관련도 내림차순.
    점수 척도(2026-09-29 확인): sentence-transformers 6.x 의 CrossEncoder 는 activation_fn=None 이면 기본값 Sigmoid 를
    걸어 확률(0~1)을 내고, 여기서 sigmoid 를 한 번 더 건다 → 관련도 = sigmoid(sigmoid(원점수)), 범위 0.5~0.73.
    문턱 MIN_RELEVANCE(0.55)는 이 척도에서 정해졌으므로 척도를 바꾸지 않는다. 젯슨은 원점수(로짓)만 돌려주고
    같은 변환을 여기서 해, 라이브러리 버전이 달라도 점수가 같게 한다."""
    texts = [rows[cid]["text"] for cid in cids]
    dev = embedder.device_config()
    if dev["url"] and dev.get("rerank"):
        scores = [_sig(_sig(x)) for x in _remote_rerank(query, texts, dev)]
    else:   # 장비 리랭커를 켜기 전까지(설치 전)는 지금처럼 PC 로
        model = _get_reranker()
        probs = model.predict([[query, t] for t in texts], convert_to_numpy=True, show_progress_bar=False)
        scores = [_sig(float(x)) for x in probs]
    order = sorted(range(len(cids)), key=lambda i: -scores[i])
    return [(cids[i], scores[i]) for i in order]
KEEP_TAGS = {"NNG", "NNP", "NR", "VV", "VA", "XR", "SL", "SH", "SN"}   # 명사·수사·동사/형용사 어근·외국어·한자·숫자
# 영어 약어·숫자용 보강 토큰(2026-09-22 추가). Kiwi 는 한국어 형태소 분석기라 영어 문서에서 "CET1"을
# "cet"+"1"로 쪼개거나 "quarter."처럼 문장 끝 낱말에 마침표를 남기는 등 토큰이 깨지는 경우가 실제로 있다
# (10-K 시험 문서로 확인). 정규식으로 알파벳(+붙은 숫자·하이픈)과 숫자(소수점·%)를 그대로 한 토큰씩 더 뽑아
# Kiwi 결과에 합친다 — 기존 한국어 토큰은 그대로 두고 보강만 한다(중복은 집합이 아니라 순서 보존 제거).
_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)?|\d+(?:\.\d+)?%?")

_kiwi = None
_kiwi_lock = threading.Lock()


def tokenize(text: str) -> list[str]:
    global _kiwi
    with _kiwi_lock:
        if _kiwi is None:
            from kiwipiepy import Kiwi

            _kiwi = Kiwi()
        kiwi_tokens = [t.form.lower() for t in _kiwi.tokenize(text) if t.tag in KEEP_TAGS]
    term_tokens = [m.group(0).lower() for m in _TERM_RE.finditer(text)]
    return list(dict.fromkeys(kiwi_tokens + term_tokens))


# ---------------------------------------------------------------- 키워드 색인 (메모리, 캐릭터별)

_bm25: dict[str, tuple[tuple[int, int], BM25Okapi, list[int]]] = {}
_bm25_lock = threading.Lock()


def invalidate(agent_id: str) -> None:
    with _bm25_lock:
        _bm25.pop(agent_id, None)


def _bm25_for(agent_id: str):
    sig = store.ready_signature(agent_id)
    with _bm25_lock:
        hit = _bm25.get(agent_id)
        if hit and hit[0] == sig:
            return hit
    rows = store.ready_chunk_texts(agent_id)
    if not rows:
        return None
    # 토큰이 하나도 없는 조각(기호뿐인 글)이 있어도 색인이 깨지지 않게 자리 표시를 넣는다
    corpus = [tokenize(text) or ["_"] for _, text in rows]
    entry = (sig, BM25Okapi(corpus), [cid for cid, _ in rows])
    with _bm25_lock:
        _bm25[agent_id] = entry
    return entry


def _keyword_search(agent_id: str, query: str, k: int, allowed_ids: set[int] | None = None) -> list[tuple[int, float]]:
    entry = _bm25_for(agent_id)
    tokens = tokenize(query)
    if not entry or not tokens:
        return []
    _, bm, ids = entry
    scores = bm.get_scores(tokens)
    candidates = (i for i in range(len(ids)) if allowed_ids is None or ids[i] in allowed_ids)
    order = sorted(candidates, key=lambda i: scores[i], reverse=True)[:k]
    return [(ids[i], float(scores[i])) for i in order if scores[i] > 0]


# ---------------------------------------------------------------- 검색

_CHITCHAT = re.compile(r"^(안녕|하이|헬로|hi\b|hello|고마|감사|땡큐|thank|네+$|넵|예$|응$|아니|그래$|알겠|알았|좋아|오케이|ok\b|okay|잘 ?있어|수고|ㅎㅎ|ㅋㅋ|ㅇㅋ|ㅇㅇ)", re.I)


def make_query(user_messages: list[str]) -> str:
    """검색어. 마지막 질문이 "그럼 두 번째는?"처럼 짧으면 앞 질문을 함께 써서 무엇에 대한 말인지 살린다.
    다만 인사·감사·짧은 응답("고마워요", "네")은 앞 질문과 합치지 않는다 — 합치면 앞 질문의 자료가 또 붙는다."""
    last = user_messages[-1].strip()
    if len(last) < 15 and len(user_messages) > 1 and not _CHITCHAT.match(last):
        return (user_messages[-2].strip() + " " + last)[:500]
    return last[:500]


def retrieve(agent_id: str, query: str, k: int = TOP_K, ticker: str | None = None, doc_type: str | None = None,
             as_of: str | None = None) -> list[dict]:
    """질문과 관련된 조각을 관련도 순으로 최대 k개. 자료가 없거나 관련된 것이 없으면 빈 목록.
    ticker/doc_type: 이 문서 메타데이터와 맞는 문서의 조각만 본다(RAG 개선 4번 — 다른 회사 내용이 안 섞이게).
    문서에 그 메타데이터가 없으면(옛 문서·정보표 없는 문서) 그 문서는 필터를 걸면 제외된다."""
    if not store.has_ready(agent_id):
        return []
    # search_excluded 문서(개별 지정, 2026-09-23)는 ticker/doc_type/as_of 지정 여부와 무관하게 항상 뺀다 —
    # 그래서 필터를 하나도 안 걸어도(allowed 최적화 없이) 매번 doc_ids_matching 을 거친다.
    allowed = store.doc_ids_matching(agent_id, ticker, doc_type, as_of)
    if not allowed:
        return []
    vec = store.vector_search(agent_id, embedder.embed_query(query), CANDIDATES, doc_ids=allowed)
    kw = _keyword_search(agent_id, query, CANDIDATES, allowed_ids=store.chunk_ids_for_docs(agent_id, allowed))

    sim = dict(vec)
    bm = dict(kw)
    fused: dict[int, float] = defaultdict(float)
    for rank, (cid, _) in enumerate(vec):
        fused[cid] += 1.0 / (RRF_K + rank + 1)
    for rank, (cid, _) in enumerate(kw):
        fused[cid] += 1.0 / (RRF_K + rank + 1)

    def relevant(cid: int) -> bool:
        if sim.get(cid, 0.0) >= MIN_VECTOR_SIM:
            return True
        # 벡터 후보(상위 20개)에 없던 조각은 유사도를 모르므로 키워드 점수만 본다
        return bm.get(cid, 0.0) >= MIN_BM25_SCORE and sim.get(cid, 1.0) >= MIN_SIM_WITH_KEYWORD

    ranked = [cid for cid in sorted(fused, key=fused.get, reverse=True) if relevant(cid)]
    rows = store.chunk_rows(agent_id, ranked)   # 아직 처리 중인 문서의 조각은 여기서 빠진다
    ranked = [cid for cid in ranked if cid in rows]
    if not ranked:
        return []

    if USE_RERANKER:
        reranked = rerank(query, ranked, rows)   # [(cid, 관련도 0~1)] — 이미 관련도 내림차순
        hits = []
        for cid, score in reranked:
            if score < MIN_RELEVANCE or len(hits) >= k:
                break   # 내림차순이라 이 아래로는 전부 기준 미달
            r = rows[cid]
            hits.append({"n": len(hits) + 1, "chunk_id": cid, "doc_id": r["doc_id"], "filename": r["filename"], "page": r["page"],
                        "published_at": r.get("published_at"),
                        "text": r["text"], "sim": round(sim[cid], 4) if cid in sim else None, "bm25": round(bm[cid], 2) if cid in bm else None,
                        "score": round(fused.get(cid, 0.0), 5), "rerank": round(score, 4)})
        return hits

    hits = []
    for cid in ranked:
        if len(hits) < k:
            r = rows[cid]
            hits.append({
                "n": len(hits) + 1, "chunk_id": cid, "doc_id": r["doc_id"], "filename": r["filename"], "page": r["page"],
                "published_at": r.get("published_at"),
                "text": r["text"], "sim": round(sim[cid], 4) if cid in sim else None, "bm25": round(bm[cid], 2) if cid in bm else None,
                "score": round(fused[cid], 5),
            })
    return hits
