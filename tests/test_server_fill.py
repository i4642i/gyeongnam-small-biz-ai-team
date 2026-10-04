"""서버가 채우는 값(작성 시각·제출일)과 인용 원문 대조."""

import json

from app.runs import checks, server_fill


def _answer(evidence):
    data = {"written_at_kst": "2026-09-27T09:00:00+09:00", "evidence": evidence}
    return "## A. 요약\n\n본문\n\n```json\n" + json.dumps(data, ensure_ascii=False) + "\n```\n"


def _run(monkeypatch):
    chunks = {7: ([(101, server_fill._norm("Net interest income increased $65.5 million, or 14.0%, and")),
                   (102, server_fill._norm("Commercial and industrial | | $ | 44,138 | | 0.31 | %"))], "2026-07-30")}
    monkeypatch.setattr(server_fill, "_doc_chunks", lambda owner, doc_id: chunks[doc_id])
    return {"agent_id": "a_x", "sources": [{"tool": "office_computer", "ok": True,
                                            "items": [{"doc_id": 7, "library_id": "l_1", "date": "2026-07-30"}]}]}


def test_fills_time_and_filed_date_and_marks_spliced_quote(monkeypatch):
    run = _run(monkeypatch)
    text = _answer([
        {"evidence_id": "E-001", "quote": "Net interest income increased $65.5 million, or 14.0%", "filed_date": None, "chunk_id": None},
        {"evidence_id": "E-002", "quote": "Commercial and industrial | 44,138 | 0.31", "filed_date": None, "chunk_id": None},
        {"evidence_id": "E-003", "quote": "Net interest income ... and increased $202.2 million", "filed_date": None, "chunk_id": None},
    ])
    out, warns = server_fill.apply(text, run)
    data, *_ = checks.locate_json(out)
    assert data["written_at_kst"] != "2026-09-27T09:00:00+09:00"
    ev = {e["evidence_id"]: e for e in data["evidence"]}
    assert ev["E-001"]["filed_date"] == "2026-07-30" and ev["E-001"]["chunk_id"] == "101"
    assert ev["E-002"]["filed_date"] == "2026-07-30"          # 표 칸 구분선·$·% 차이는 같은 인용으로 본다
    assert ev["E-003"]["filed_date"] is None                  # 중간을 잘라 이은 인용은 채우지 않고 표시만
    assert run["quote_check"] == {"checked": 3, "missing": ["E-003"]}
    assert warns and "E-003" in warns[0]
    assert "## 서버 인용 대조" in out and "\n```" in out


def test_no_json_or_no_documents_leaves_text(monkeypatch):
    assert server_fill.apply("글만 있음", {"sources": []}) == ("글만 있음", [])
    text = "```json\n" + json.dumps({"evidence": [{"evidence_id": "E-001", "quote": "something long enough here"}]}) + "\n```"
    out, warns = server_fill.apply(text, {"agent_id": "a_x", "sources": []})
    assert out == text and warns == []


# ---- 경고 통과 허용 목록(app/runs/soft.py)

from app.runs import soft


def test_soft_allowlist_only_format_items():
    assert soft.is_soft("written_at_kst: None is not of type 'string'")
    assert soft.is_soft("evidence/3/filed_date: None is not of type 'string'")
    assert soft.is_soft("[format] evidence/0/chunk_id: None is not of type 'string'")
    assert soft.is_soft("[soft] E-007: 인용이 31단어 — 30단어 이하여야 합니다.")
    assert not soft.is_soft("checks/0/finding: None is not of type 'string'")        # 핵심 내용 칸은 대상 아님
    assert not soft.is_soft("E-007: 인용이 60단어 — 30단어 이하여야 합니다.")           # 상한 넘는 인용(훅이 [soft] 안 붙임)
    assert not soft.is_soft("E-013: 인용이 이번 실행에서 연 문서 원문에 이어진 그대로 없습니다")  # 원문 불일치 = 내용 오류
    assert soft.all_soft(["[soft] a", "written_at_kst: None is not of type 'string'"])
    assert not soft.all_soft(["[soft] a", "overall.verdict 가 규칙 결과와 다릅니다"])
    assert not soft.all_soft(["[soft] a", "… 외 3건"])
    assert not soft.all_soft([])
    assert soft.label("[soft] E-007: 인용이 31단어") == "[경고 통과] E-007: 인용이 31단어"


def test_quote_problems_lists_only_missing(monkeypatch):
    run = _run(monkeypatch)
    text = _answer([
        {"evidence_id": "E-001", "quote": "Net interest income increased $65.5 million, or 14.0%"},
        {"evidence_id": "E-002", "quote": "Net interest income ... and increased $202.2 million"},
    ])
    probs = server_fill.quote_problems(text, run)
    assert len(probs) == 1 and probs[0].startswith("E-002:")
