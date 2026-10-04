"""graph_search 도구(지식그래프 능동 조회) 오프라인 테스트. 실제 모델·네트워크 없음 — graph_rag.augment 를
가짜로 바꿔 runner 의 형식 처리(_graph_search)만 확인한다."""

import os
import tempfile
import unittest
from unittest.mock import patch

# 다른 시험 파일과 한 명령으로 같이 돌 때 app.config.DATA_DIR 가 실제 data/ 로 굳지 않도록, 임시 폴더를
# setdefault 로 잡는다(이 파일은 store 를 직접 안 쓰지만 — augment 를 가짜로 바꾼다 — DATA_DIR 결정에는 참여한다).
if "AGENT_TOWN_DATA" not in os.environ:
    _sandbox = tempfile.TemporaryDirectory(prefix="agent-town-graphtool-")
    os.environ["AGENT_TOWN_DATA"] = _sandbox.name
os.environ.setdefault("AGENT_TOWN_NO_DOTENV", "1")

from app.tools import registry, runner


class GraphSearchFormat(unittest.TestCase):
    def test_registry_accepts_graph_search_format(self):
        defn = registry.ToolDef(
            id="graph_search_test", label="G", description="지식그래프 조회 도구", read_only=True,
            params=[registry.Param(name="query", type="string", description="개체·주제", required=True)],
            http=registry.Http(method="GET", url="https://graph-search.invalid/", headers={}, send="query", extra={}),
            format="graph_search",
        )
        self.assertEqual(defn.format, "graph_search")

    def test_registry_requires_query_param(self):
        with self.assertRaises(ValueError):
            registry.ToolDef(
                id="graph_bad", label="G", description="query 없음", read_only=True,
                params=[registry.Param(name="topic", type="string", description="x", required=True)],
                http=registry.Http(method="GET", url="https://graph-search.invalid/", headers={}, send="topic", extra={}),
                format="graph_search",
            )


class GraphSearchExecution(unittest.TestCase):
    def _ctx(self):
        return runner.Context("a_00000000", mode="chat", inputs={})

    @patch("app.rag.graph_rag.augment")
    def test_returns_entities_relations_and_evidence(self, mock_augment):
        mock_augment.return_value = {
            "entities": ["Reuters", "통신사"],
            "info": ['분류 "통신사" 아래 항목(3개): Reuters, Bloomberg, Dow Jones',
                     '관계: "Reuters" — 재인용됨 — "국내 증권사"'],
            "extra": [{"chunk_id": 5, "doc_id": 1, "filename": "출처.md", "page": None, "text": "Reuters 는 통신사다.", "published_at": None}],
            "semantic": [],
        }
        text, items = runner._graph_search({"query": "통신사"}, self._ctx())
        self.assertIn("관련 개체: Reuters, 통신사", text)
        self.assertIn("통신사", text)
        self.assertIn("[W1]", text)          # 근거 조각이 W 태그로 붙는다
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "출처.md")

    @patch("app.rag.graph_rag.augment")
    def test_empty_graph_returns_friendly_message(self, mock_augment):
        mock_augment.return_value = {"entities": [], "info": [], "extra": [], "semantic": []}
        text, items = runner._graph_search({"query": "없는것"}, self._ctx())
        self.assertIn("찾지 못했습니다", text)
        self.assertEqual(items, [])

    def test_blank_query_rejected(self):
        text, items = runner._graph_search({"query": "  "}, self._ctx())
        self.assertIn("비어 있습니다", text)
        self.assertEqual(items, [])


if __name__ == "__main__":
    unittest.main()
