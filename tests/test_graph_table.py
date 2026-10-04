"""지식 표(kg_*.md) → 그래프 행 읽기(app/rag/graph_table.py). 모델을 부르지 않는다."""

from app.rag import chunking, extract, graph_table

DOC = """# 매체 신뢰도 표

| 개체 | 개체 종류 | 관계 | 대상 | 대상 종류 | 설명 |
|---|---|---|---|---|---|
| 통신사 | 개념 | 포함 | Reuters | 조직 | 사실 확인이 빠른 1차 보도 |
| Reuters | 조직 | 신뢰도 | 높음 | 개념 | 원보도 위주 |
| 보도자료 배포처 | 개념 | 포함 | GlobeNewswire | 조직 | 회사가 직접 낸 글 |
| MarketBeat | 조직 |  |  |  | 자동 생성 기사 많음 |
"""


def test_parse_rows_and_graph():
    rows = graph_table.parse_rows(DOC)
    assert len(rows) == 4 and rows[0]["개체"] == "통신사" and rows[3]["관계"] == ""
    ents, rels = graph_table.rows_to_graph(rows)
    names = {e["name"] for _, e in ents}
    assert {"통신사", "Reuters", "높음", "GlobeNewswire", "MarketBeat"} <= names
    assert any(r["label"] == "포함" for r in rels) and any(r["label"] == "신뢰도" for r in rels)
    reuters = next(e for _, e in ents if e["name"] == "Reuters")
    assert reuters["type"] == "조직" and "원보도" in reuters["desc"]


def test_long_table_chunks_keep_header(tmp_path):
    body = "\n".join(f"| 회사{i} | 조직 | 별칭 | 별칭{i} | 조직 | 설명 {i} 입니다 조금 길게 적어 둡니다 |" for i in range(200))
    p = tmp_path / "kg_alias.md"
    p.write_text(DOC.split("| 통신사")[0] + body + "\n", encoding="utf-8")
    pages, _ = extract.extract(p, ".md")
    chunks = chunking.make_chunks(pages)
    assert len(chunks) > 1
    total = sum(len(graph_table.parse_rows(t)) for _, t in chunks)
    assert total == 200


def test_is_table_doc():
    assert graph_table.is_table_doc("kg_media.md") and not graph_table.is_table_doc("guide.md")
