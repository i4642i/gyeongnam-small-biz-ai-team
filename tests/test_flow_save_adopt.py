"""흐름 저장 동시성과 '실행은 끝났는데 기록을 놓친 책상' 거두기(2026-09-27 ASB 사고)."""

import json
import threading

from app.offices import flow as fl


def test_concurrent_save_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(fl, "_path", lambda office_id, flow_id: tmp_path / f"{flow_id}.json")
    flow = {"id": "f_test0001", "office_id": "o_x", "departments": [], "outputs": {}, "n": 0}
    errors = []

    def worker(i):
        try:
            for k in range(30):
                flow["outputs"][f"{i}:{k}"] = k      # 다른 스레드가 고치는 중에도 저장
                fl._save(flow)
        except Exception as e:                       # 예전에는 FileNotFoundError 로 스레드가 죽었다
            errors.append(e)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    assert len(json.loads((tmp_path / "f_test0001.json").read_text(encoding="utf-8"))["outputs"]) == 180
    assert not list(tmp_path.glob("*.tmp"))


def test_adopt_finished_records_result_without_rerun(monkeypatch):
    finished = {"status": "ok", "stage": "완료", "answer": '```json\n{"ticker": "ASB"}\n```', "sources": [], "usage": {"calls": 3}}
    monkeypatch.setattr(fl.runs, "get", lambda agent_id, run_id: finished if run_id == "r_done" else {"status": "running"})
    monkeypatch.setattr(fl, "_attach_evidence", lambda *a, **k: None)
    flow = {"outputs": {}}
    desks = [{"key": "d:2", "agent_id": "a", "run_id": "r_done", "status": "running"},
             {"key": "d:3", "agent_id": "a", "run_id": "r_live", "status": "running"},
             {"key": "d:4", "agent_id": "a", "run_id": None, "status": "pending"}]
    assert fl._adopt_finished(flow, desks) == 1
    assert desks[0]["status"] == "ok" and flow["outputs"]["d:2"] == {"ticker": "ASB"}
    assert desks[1]["status"] == "running" and desks[2]["status"] == "pending"
