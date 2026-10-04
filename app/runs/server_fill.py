"""모델에게 쓰게 두지 않는 값을 서버가 채우고, 인용문을 원문과 대조한다(2026-09-27).

- written_at_kst: 결과 JSON 에 이 칸이 있으면 실제 작성 시각(한국 시간)으로 덮어쓴다(모델이 지어낸 시각 방지).
- evidence[].quote: 이번 실행에서 문서 검색 도구(캐릭터 문서함·회사 컴퓨터)로 열어 본 문서의 원문과 대조한다.
  원문에 이어진 그대로 있으면 filed_date(그 문서의 제출일)·chunk_id 가 비어 있을 때 채우고,
  없으면(중간을 잘라 이었거나 지어낸 인용) 경고와 결과 끝의 '서버 인용 대조' 절에 표시한다.
검증(스키마·훅)이 끝난 뒤에 적용한다 — 판정·재요청에는 영향을 주지 않고, 값만 바로잡고 표시만 한다.
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import closing
from datetime import datetime, timedelta, timezone

from app.runs import checks
from app.runs.evidence import RAG_FORMATS
from app.tools import registry

log = logging.getLogger("agent_town.runs")

KST = timezone(timedelta(hours=9))
MIN_QUOTE = 12   # 정규화 뒤 이보다 짧은 인용은 대조하지 않는다(숫자 하나 등 — 어디에나 있을 수 있음)
_KEEP = re.compile(r"[^0-9a-z가-힣]+")
_SWAP = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "})


def _norm(s: str) -> str:
    """글자·숫자만 남긴다 — 표의 칸 구분선(|)·$·%·공백·따옴표 모양 차이는 무시하고, 낱말이 빠지거나 바뀐 것만 잡는다."""
    return _KEEP.sub("", str(s).translate(_SWAP).lower())


def _retrieved(sources: list[dict], agent_id: str) -> dict[tuple[str, int], str | None]:
    """(문서 주인, 문서 번호) → 검색 결과에 붙어 온 제출일. 이번 실행에서 실제로 연 문서만."""
    out: dict[tuple[str, int], str | None] = {}
    for s in sources or []:
        t = registry.get(s.get("tool", ""))
        if not s.get("ok") or t is None or t.format not in RAG_FORMATS:
            continue
        for it in s.get("items") or []:
            if isinstance(it, dict) and it.get("doc_id") is not None:
                key = (it.get("library_id") or agent_id, int(it["doc_id"]))
                out[key] = out.get(key) or it.get("date")
    return out


def _doc_chunks(owner: str, doc_id: int) -> tuple[list[tuple[int, str]], str | None]:
    from app.rag import store
    with closing(store._connect(owner)) as conn:
        rows = conn.execute("SELECT id, text FROM chunks WHERE doc_id = ? ORDER BY ord", (doc_id,)).fetchall()
        d = conn.execute("SELECT published_at FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return [(r["id"], _norm(r["text"])) for r in rows], (d["published_at"] if d else None)


def _check_quotes(data: dict, sources: list[dict], agent_id: str) -> list[dict]:
    """evidence 마다 {id, found, filed_date, chunk_id}. 대조할 문서가 없으면 빈 목록."""
    docs = _retrieved(sources, agent_id)
    ev = [e for e in (data.get("evidence") or []) if isinstance(e, dict) and isinstance(e.get("quote"), str)]
    if not docs or not ev:
        return []
    texts = []
    for (owner, doc_id), date in docs.items():
        try:
            chunks, published = _doc_chunks(owner, doc_id)
        except Exception:
            log.exception("인용 대조용 문서를 읽지 못했습니다 — %s/%s", owner, doc_id)
            continue
        texts.append((chunks, "".join(t for _, t in chunks), (date or published or "")[:10] or None))
    out = []
    for e in ev:
        q = _norm(e["quote"])
        if len(q) < MIN_QUOTE:
            continue
        hit = None
        for chunks, whole, date in texts:
            if q in whole:
                cid = next((c for c, t in chunks if q in t), None)   # 조각 경계에 걸치면 조각 번호는 비워 둔다
                hit = {"filed_date": date, "chunk_id": cid}
                break
        out.append({"id": e.get("evidence_id", "?"), "found": hit is not None, **(hit or {})})
    return out


def quote_problems(text: str, run: dict) -> list[str]:
    """업무가 '인용은 이번 실행에서 연 문서 원문 그대로'(quote_source=documents)일 때 검사 단계에서 쓰는 내용 오류 목록.
    원문에 이어진 그대로 없는 인용마다 한 줄 — 재요청으로 고칠 기회를 주고, 끝내 안 고쳐지면 실패(경고 통과 대상 아님)."""
    data, _, _, err = checks.locate_json(text)
    if err or not isinstance(data, dict):
        return []
    return [f"{r['id']}: 인용이 이번 실행에서 연 문서 원문에 이어진 그대로 없습니다 — 도구 결과에 나온 원문을 낱말 하나 바꾸지 말고 "
            "이어진 구간 그대로 옮기세요(\"...\"로 잇거나 중간을 빼면 안 됩니다. 두 곳이 필요하면 근거를 둘로 나누세요)."
            for r in _check_quotes(data, run.get("sources") or [], run.get("agent_id") or "") if not r["found"]]


def apply(text: str, run: dict) -> tuple[str, list[str]]:
    """(고친 결과 글, 경고). JSON 이 없거나 읽지 못하면 그대로 돌려준다."""
    data, start, end, err = checks.locate_json(text)
    if err or not isinstance(data, dict) or start is None:
        return text, []
    warnings: list[str] = []
    changed = False
    if "written_at_kst" in data:
        data["written_at_kst"] = datetime.now(KST).isoformat(timespec="seconds")
        changed = True
    results = _check_quotes(data, run.get("sources") or [], run.get("agent_id") or "")
    by_id = {r["id"]: r for r in results}
    for e in data.get("evidence") or []:
        r = by_id.get(e.get("evidence_id")) if isinstance(e, dict) else None
        if not r or not r["found"]:
            continue
        if "filed_date" in e and not e.get("filed_date") and r.get("filed_date"):
            e["filed_date"] = r["filed_date"]
            changed = True
        if "chunk_id" in e and not e.get("chunk_id") and r.get("chunk_id") is not None:
            e["chunk_id"] = str(r["chunk_id"])
            changed = True
    if changed:
        tail = text[end:]
        text = text[:start] + json.dumps(data, ensure_ascii=False, indent=2) + ("" if tail.startswith("\n") else "\n") + tail
    missing = [r["id"] for r in results if not r["found"]]
    if results:
        run["quote_check"] = {"checked": len(results), "missing": missing}
        lines = [f"\n\n## 서버 인용 대조\n\n이번 실행에서 연 문서 원문과 인용 {len(results)}개를 대조했습니다(서버가 붙인 절입니다)."]
        if missing:
            lines.append(f"**원문에 이어진 그대로 없는 인용: {', '.join(missing)}** — 중간을 잘라 이었거나 원문과 다른 인용입니다.")
            warnings.append(f"[citation_error] 원문에 그대로 없는 인용: {', '.join(missing)}")
        else:
            lines.append("모두 원문에 그대로 있습니다.")
        text = text.rstrip() + "\n".join(lines) + "\n"
    return text, warnings
