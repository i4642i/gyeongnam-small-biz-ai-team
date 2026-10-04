"""회사 컴퓨터 수집 작업·검증 근거 스냅샷·예전 임시 캐릭터 뒷정리 오프라인 시험.
가짜 SEC 응답·가짜 임베딩(모델을 안 부르고 바로 ready 로 표시)만 쓴다. 실제 네트워크·LLM 호출 없음.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

if "AGENT_TOWN_DATA" not in os.environ:
    _sandbox = tempfile.TemporaryDirectory(prefix="agent-town-collect-")
    os.environ["AGENT_TOWN_DATA"] = _sandbox.name
os.environ.setdefault("AGENT_TOWN_NO_DOTENV", "1")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from app.agents import store as agents
from app.libraries import collect, store as libs
from app.offices import flow as office_flow
from app.rag import chunking, extract
from app.rag import store as rag_store

CIK = 9990001
SUBMISSIONS = {"filings": {"recent": {
    "form": ["10-K", "10-Q"], "accessionNumber": ["0002-26-000001", "0002-26-000002"],
    "filingDate": ["2026-02-01", "2026-08-01"], "primaryDocument": ["co-10k.htm", "co-10q.htm"],
}}}
FAKE_HTML = b"<html><body><p>Item 1. Business</p><p>Test body.</p></body></html>"


def _fake_get(url: str, retries: int = 3) -> bytes:
    return json.dumps(SUBMISSIONS).encode() if "submissions/CIK" in url else FAKE_HTML


def _instant_ready():
    """색인 줄 넣기를 실제 임베딩 없이 바로 ready 로 표시(추출→조각→정보표 반영은 실제와 같게)."""
    def fake_enqueue(owner_id: str, doc_id: int) -> None:
        doc = rag_store.get_document(owner_id, doc_id)
        pages, empty = extract.extract(rag_store.docs_dir(owner_id) / doc["stored_name"], doc["ext"])
        chunks = chunking.make_chunks(pages)
        rag_store.replace_chunks(owner_id, doc_id, chunks)
        meta = chunking.doc_meta(pages[0].text) if pages else {}
        date_keys = ("published_at", "date_status", "date_basis", "date_evidence", "event_date")
        date_meta = {k: meta.pop(k) for k in date_keys if k in meta}
        rag_store.update_document(owner_id, doc_id, status="ready", pages=len(pages), chunks=len(chunks), empty_pages=empty, **meta)
        if date_meta:
            rag_store.update_date_meta(owner_id, doc_id, **date_meta)
    return patch("app.offices.rag_prep.rag_worker.enqueue", side_effect=fake_enqueue)


def _sec(tickers=None):
    return (patch("tools.collect_sec_filings._get", side_effect=_fake_get),
            patch("tools.collect_sec_filings._company_tickers_map", return_value=tickers or {"CO": (CIK, "Company Inc")}))


class LegacyEphemeralCleanupTests(unittest.TestCase):
    """예전 방식 임시 캐릭터 뒷정리 안전장치: 표시 없는 진짜 캐릭터는 절대 안 지운다."""

    def _agent(self):
        return agents.create("검증", "역", "", "p", {}, {"provider": "anthropic", "model": "claude-fake-1"},
                             {"output_format": "", "allow_free_requests": True, "tasks": []}, [])["id"]

    def test_delete_refuses_real_agent(self):
        real = self._agent()
        with self.assertRaises(ValueError):
            agents.delete_ephemeral(real)
        agents.get(real)

    def test_legacy_leftover_is_listed_and_deletable(self):
        aid = self._agent()
        d = agents.agent_dir(aid)
        info = json.loads((d / "agent.json").read_text(encoding="utf-8"))
        info["internal_run"] = {"source_agent_id": "a_00000000", "flow_id": "f_old00001", "desk_key": "d:0"}
        (d / "agent.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        self.assertIn(aid, {a["id"] for a in agents.list_ephemeral(flow_id="f_old00001")})
        self.assertNotIn(aid, {a["id"] for a in agents.list_all()})
        agents.delete_ephemeral(aid)
        with self.assertRaises(agents.AgentNotFound):
            agents.get(aid)


if __name__ == "__main__":
    unittest.main()
