"""컴퓨터(회사 공용 문서함) 오프라인 시험. 모델·임베딩 호출 없음(문서 처리 줄 세우기는 가로챈다).

검증 항목:
 - 보관 자료 목록·글·조각 보기, 검색 제외
 - 색인 장비 설정(토큰은 다시 보여 주지 않음)
 - 없는 컴퓨터는 404
"""

import os
import tempfile
import unittest
from unittest.mock import patch

if "AGENT_TOWN_DATA" not in os.environ:
    _sandbox = tempfile.TemporaryDirectory(prefix="agent-town-lib-")
    os.environ["AGENT_TOWN_DATA"] = _sandbox.name
os.environ.setdefault("AGENT_TOWN_NO_DOTENV", "1")
os.environ.setdefault("LLM_MODE", "mock")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import LIBRARIES_DIR
from app.libraries import api as libraries_api
from app.offices import store as offices
from app.rag import api as rag_api, worker


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(rag_api.router)
    app.include_router(libraries_api.router)
    return TestClient(app)


class LibraryApiTests(unittest.TestCase):
    def setUp(self):
        self.c = _client()
        self.enq = patch.object(worker, "enqueue").start()
        self.addCleanup(patch.stopall)

    def _make(self, name="뉴스 수집 컴퓨터"):
        from app.libraries import store as lib_store
        return lib_store.create(name, "", [])

    def test_inventory_text_and_exclusion(self):
        from app.rag import store as rag_store
        lib = self._make()
        doc_id, stored = rag_store.add_document(lib["id"], "ABC_10Q.md", ".md", 30, "sha-abc")
        (rag_store.docs_dir(lib["id"]) / stored).write_text("| 티커 | ABC |\n\nNet interest margin rose.", encoding="utf-8")
        rag_store.update_document(lib["id"], doc_id, status="ready", ticker="ABC", doc_type="10-Q", period="2026-08", chunks=1)
        inv = self.c.get(f"/api/libraries/{lib['id']}/inventory").json()
        self.assertEqual(inv["summary"]["documents"], 1)
        self.assertEqual(inv["summary"]["no_date"], 1)   # 게재일 없는 문서는 기준일 검색에서 빠진다 — 관리자에게 보여 준다
        self.assertEqual(inv["documents"][0]["ticker"], "ABC")
        self.assertIsNone(inv["documents"][0]["origin"])   # 작업이 아니라 직접 올린 문서
        t = self.c.get(f"/api/libraries/{lib['id']}/documents/{doc_id}/text").json()
        self.assertIn("Net interest margin", t["text"])
        self.assertEqual(self.c.post(f"/api/libraries/{lib['id']}/documents/{doc_id}/exclude", json={"reason": ""}).status_code, 422)
        self.assertEqual(self.c.post(f"/api/libraries/{lib['id']}/documents/{doc_id}/exclude", json={"reason": "옛 분기"}).status_code, 200)
        inv = self.c.get(f"/api/libraries/{lib['id']}/inventory").json()
        self.assertTrue(inv["documents"][0]["search_excluded"])
        self.assertEqual(inv["summary"]["excluded"], 1)
        self.c.post(f"/api/libraries/{lib['id']}/documents/{doc_id}/include")
        self.assertFalse(self.c.get(f"/api/libraries/{lib['id']}/inventory").json()["documents"][0]["search_excluded"])
        self.assertEqual(self.c.get(f"/api/libraries/{lib['id']}/documents/9999/text").status_code, 404)

    def test_chunks_and_files_views(self):
        from app.rag import store as rag_store
        lib = self._make()
        doc_id, stored = rag_store.add_document(lib["id"], "ABC_10Q.md", ".md", 30, "sha-files")
        (rag_store.docs_dir(lib["id"]) / stored).write_text("hello", encoding="utf-8")
        rag_store.replace_chunks(lib["id"], doc_id, [(None, "첫 조각 글"), (None, "둘째 조각 글")])
        c = self.c.get(f"/api/libraries/{lib['id']}/documents/{doc_id}/chunks").json()
        self.assertEqual(c["total"], 2)
        self.assertEqual([x["n"] for x in c["chunks"]], [1, 2])
        self.assertFalse(c["chunks"][0]["vector"])   # 벡터는 아직 없다(색인 전)
        files = self.c.get(f"/api/libraries/{lib['id']}/files").json()["files"]
        paths = {f["path"] for f in files}
        self.assertIn("library.json", paths)
        self.assertIn(f"docs/{stored}", paths)
        self.assertEqual(next(f for f in files if f["path"] == f"docs/{stored}")["doc"]["filename"], "ABC_10Q.md")
        self.assertEqual(self.c.get(f"/api/libraries/{lib['id']}/files/content", params={"path": "library.json"}).json()["text"][:1], "{")
        self.assertEqual(self.c.get(f"/api/libraries/{lib['id']}/files/content", params={"path": "agent.db"}).status_code, 415)
        self.assertEqual(self.c.get(f"/api/libraries/{lib['id']}/files/content", params={"path": "../../offices/x.json"}).status_code, 404)

    def test_index_device_settings_hide_token(self):
        from app.rag import embedder
        self.assertEqual(self.c.put("/api/index-device", json={"url": "not a url"}).status_code, 422)
        r = self.c.put("/api/index-device", json={"url": "http://127.0.0.1:9", "token": "secret-x"}).json()
        self.assertEqual(r["url"], "http://127.0.0.1:9")
        self.assertTrue(r["token_set"])
        self.assertNotIn("secret-x", str(self.c.get("/api/index-device").json()))   # 토큰은 다시 보여 주지 않는다
        r = self.c.put("/api/index-device", json={"url": "http://127.0.0.1:9"}).json()   # 토큰을 안 보내면 그대로 둔다
        self.assertEqual(embedder.device_config()["token"], "secret-x")
        t = self.c.post("/api/index-device/test", json={}).json()   # 포트 9 는 닫혀 있다 — 실패를 알려 주고 예외는 안 난다
        self.assertFalse(t["ok"])
        self.assertTrue(t["problem"])
        self.c.put("/api/index-device", json={"url": ""})
        self.assertEqual(self.c.get("/api/index-device").json()["state"], "off")

    def test_index_device_defaults_to_local(self):
        """화면에서 고른 적이 없으면 .env 에 젯슨 주소가 있어도 이 PC 로 계산한다(주소는 보관만, 2026-10-04)."""
        import json
        from pathlib import Path
        from app.rag import embedder
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "index_device.json"
        patch.object(embedder, "DEVICE_PATH", path).start()
        with patch.dict(os.environ, {"EMBED_REMOTE_URL": "http://127.0.0.1:9", "EMBED_REMOTE_TOKEN": "t"}):   # 설정 파일 없음 + .env 주소 있음
            cfg = embedder.device_config()
            self.assertEqual((cfg["url"], cfg["saved_url"], cfg["use_local"]), ("", "http://127.0.0.1:9", True))
            v = self.c.get("/api/index-device").json()
            self.assertEqual((v["url"], v["state"]), ("", "off"))
            t = self.c.post("/api/index-device/test", json={}).json()   # 이 PC 모드에서도 보관된 주소로 시험한다
            self.assertNotEqual(t.get("detail"), "시험할 주소가 없습니다.")
            self.assertFalse(t["ok"])
            path.write_text(json.dumps({"url": "http://127.0.0.1:9"}), encoding="utf-8")   # use_local 키 없는 옛 설정 파일
            self.assertTrue(embedder.device_config()["use_local"])
            path.write_text(json.dumps({"url": "http://127.0.0.1:9", "use_local": False}), encoding="utf-8")   # 화면에서 [젯슨] 을 고름
            self.assertEqual(embedder.device_config()["url"], "http://127.0.0.1:9")
            path.unlink()
        with patch.dict(os.environ, {"EMBED_REMOTE_URL": ""}):   # 설정 파일 없음 + .env 주소 비어 있음
            cfg = embedder.device_config()
            self.assertEqual((cfg["url"], cfg["saved_url"], cfg["use_local"]), ("", "", True))
            self.assertEqual(self.c.put("/api/index-device/mode", json={"local": False}).status_code, 422)   # [젯슨] 은 고를 수 없다

    def test_unknown_library_is_404(self):
        self.assertEqual(self.c.get("/api/libraries/l_00000000").status_code, 404)
        self.assertEqual(self.c.get("/api/libraries/l_00000000/documents").status_code, 404)
        self.assertEqual(self.c.get("/api/libraries/../x/documents").status_code, 404)


class OfficeComputerToolTests(unittest.TestCase):
    """회사 컴퓨터 도구: 앉은 회사의 컴퓨터만 보고, 이름으로 한 대를 고를 수 있고, 여러 대면 관련도 순으로 합친다."""

    def setUp(self):
        from app.agents import store as agents
        from app.libraries import store as libs
        self.agent = agents.create("검증", "역", "", "p", {}, {"provider": "anthropic", "model": "claude-fake-1"},
                                   {"output_format": "", "allow_free_requests": True, "tasks": []}, ["office_computer"])["id"]
        self.a = libs.create("공시 컴퓨터", "", ["sec_filing", "rag_index"])
        self.b = libs.create("뉴스 컴퓨터", "", ["rag_index"])
        self.store_only = libs.create("보관 컴퓨터", "", ["sec_filing"])   # 색인 도구 없음 — 검색 대상이 아니다(2026-09-28)
        self.office = offices.create("투자", "", [{"name": "검증부", "desks": 1}])
        offices.assign(self.office["id"], self.office["departments"][0]["id"], 0, self.agent)
        offices.set_libraries(self.office["id"], [self.a["id"], self.b["id"], self.store_only["id"]])

    def test_store_only_computer_is_not_searched(self):
        text, items, calls = self._run({"query": "margin"}, {self.store_only["id"]: [self._hit(9, 0.99)]})
        self.assertNotIn(self.store_only["id"], [c[0] for c in calls])
        text, items, calls = self._run({"query": "margin", "computer": "보관 컴퓨터"}, {})
        self.assertEqual(calls, [])
        self.assertNotIn("보관 컴퓨터", text.split("쓸 수 있는 컴퓨터:")[-1])

    def _run(self, args, hits_by_lib):
        from app.rag import search
        from app.tools import runner
        calls = []

        def fake(owner, query, k=6, ticker=None, doc_type=None, as_of=None):
            calls.append((owner, ticker))
            return hits_by_lib.get(owner, [])
        with patch.object(search, "retrieve", side_effect=fake):
            text, items = runner._office_computer(args, runner.Context(agent_id=self.agent))
        return text, items, calls

    def _hit(self, doc_id, rerank):
        return {"doc_id": doc_id, "chunk_id": doc_id, "page": None, "filename": f"d{doc_id}.md", "text": f"t{doc_id}", "rerank": rerank}

    def test_agent_without_office_gets_clear_message(self):
        from app.agents import store as agents
        from app.tools import runner
        lone = agents.create("혼자", "역", "", "p", {}, {"provider": "anthropic", "model": "claude-fake-1"}, {"output_format": "", "allow_free_requests": True, "tasks": []}, [])["id"]
        text, items = runner._office_computer({"query": "q"}, runner.Context(agent_id=lone))
        self.assertEqual(items, [])
        self.assertIn("컴퓨터가 없습니다", text)


if __name__ == "__main__":
    unittest.main()
