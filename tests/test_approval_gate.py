"""프롬프트 결재 관문 오프라인 시험. 모델 호출·네트워크 없음.

검증 항목:
 - _approval_state / approve_desk / reject_desk / _clear_approvals (순수 상태 전이)
 - _stage_for_approval 가 프롬프트 파일을 쓰고 상태를 'awaiting_approval' 로 바꾸며, read_desk_prompt 로 되읽힌다
 - _build_prompt_preview 가 모델 호출 없이 시스템+유저 프롬프트를 조립한다
 - _run_department(비반복) 가 승인 전에는 runs.start 를 부르지 않고, 승인하면 전송하고, 거부하면 실패로 남긴다
"""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

if "AGENT_TOWN_DATA" not in os.environ:
    _sandbox = tempfile.TemporaryDirectory(prefix="agent-town-appr-")
    os.environ["AGENT_TOWN_DATA"] = _sandbox.name
os.environ.setdefault("AGENT_TOWN_NO_DOTENV", "1")
os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from app.offices import flow


class ApprovalStateTests(unittest.TestCase):
    def setUp(self):
        flow._clear_approvals("f_test")

    def tearDown(self):
        flow._clear_approvals("f_test")

    def test_default_is_waiting(self):
        self.assertEqual(flow._approval_state("f_test", "d:0"), "waiting")

    def test_approve_then_approved(self):
        flow.approve_desk("f_test", "d:0")
        self.assertEqual(flow._approval_state("f_test", "d:0"), "approved")
        self.assertEqual(flow._approval_state("f_test", "d:1"), "waiting")   # 다른 책상은 그대로 대기

    def test_reject_wins_over_nothing(self):
        flow.reject_desk("f_test", "d:0")
        self.assertEqual(flow._approval_state("f_test", "d:0"), "rejected")

    def test_clear_resets(self):
        flow.approve_desk("f_test", "d:0")
        flow.reject_desk("f_test", "d:1")
        flow._clear_approvals("f_test")
        self.assertEqual(flow._approval_state("f_test", "d:0"), "waiting")
        self.assertEqual(flow._approval_state("f_test", "d:1"), "waiting")


class PromptPreviewTests(unittest.TestCase):
    """모델 호출 없이 프롬프트를 조립해 파일로 남기는지 확인한다."""

    TASK = {
        "id": "t_appr",
        "name": "거시 요약",
        "description": "거시 지표 요약",
        "instruction": "관측 기간 {기간} 의 거시 지표를 요약하라.",
        "inputs": [{"name": "기간", "type": "text", "required": True}],
        "output_template": "",
    }
    AGENT = {
        "id": "a_appr0001",
        "name": "테스트 애널리스트",
        "role": "거시",
        "tagline": "",
        "persona": "당신은 신중한 거시 분석가입니다.",
        "model": {"provider": "deepseek", "model": "deepseek-flash"},
        "tools": [],
        "work": {"output_format": "", "tasks": [TASK]},
    }

    def test_build_preview_no_llm(self):
        task = self.TASK
        preview = flow._build_prompt_preview(self.AGENT, task, {"기간": "2026 3분기"})
        self.assertIn("결재용 프롬프트 미리보기", preview)
        self.assertIn("테스트 애널리스트", preview)
        self.assertIn("시스템 프롬프트", preview)
        self.assertIn("2026 3분기", preview)   # 유저 프롬프트에 입력이 렌더됐다

    def test_stage_and_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(flow, "_flows_dir", return_value=Path(td)):
                fl = {"id": "f_stage01", "office_id": "o_x"}
                x = {"key": "d_dept:0"}
                task = self.TASK
                flow._stage_for_approval(fl, x, self.AGENT, task, {"기간": "2026 3분기"})
                self.assertEqual(x["status"], "awaiting_approval")
                self.assertTrue(x["prompt_ready"])
                back = flow.read_desk_prompt("o_x", "f_stage01", "d_dept:0")
                self.assertIsNotNone(back)
                self.assertIn("2026 3분기", back)
                # 준비 안 된 책상은 None
                self.assertIsNone(flow.read_desk_prompt("o_x", "f_stage01", "d_dept:99"))


class RunDepartmentGateTests(unittest.TestCase):
    """_run_department(비반복)가 승인 전에는 절대 runs.start 를 부르지 않는다."""

    def _flow_and_dep(self):
        fl = {"id": "f_gate01", "office_id": "o_gate", "stage": "", "outputs": {}, "totals": {},
              "departments": []}
        dep = {"id": "d_dept", "name": "시장 분석", "mode": "parallel", "status": "pending",
               "desks": [{"key": "d_dept:0", "agent_id": "a_1", "agent_name": "김", "agent_role": "거시",
                          "task_id": "t", "task_name": "요약", "status": "pending", "stage": "",
                          "run_id": None, "error": None}]}
        fl["departments"] = [dep]
        return fl, dep

    def test_no_start_before_approval_then_approve(self):
        fl, dep = self._flow_and_dep()
        started = {"count": 0}

        def fake_start(agent, task, values, long_names):
            started["count"] += 1
            return {"id": "r_1", "stage": "대기"}

        def fake_get(agent_id, run_id):
            return {"status": "ok", "answer": '{"ok": true}', "duration_s": 1, "usage": {}, "sources": [], "stage": "완료"}

        with patch.object(flow, "_flows_dir") as mflow, \
             patch.object(flow, "_save"), \
             patch.object(flow.agent_store, "get", return_value={"id": "a_1"}), \
             patch.object(flow.runs, "find_task", return_value={"id": "t", "name": "요약"}), \
             patch.object(flow, "_desk_inputs", return_value=({}, [], 0)), \
             patch.object(flow, "_stage_for_approval") as mstage, \
             patch.object(flow.runs, "start", side_effect=fake_start), \
             patch.object(flow.runs, "get", side_effect=fake_get), \
             patch.object(flow.checks, "extract_json", return_value=({"ok": True}, None, None)):
            tmp = tempfile.mkdtemp()
            mflow.return_value = Path(tmp)

            def stage(fl_, x, a, t, v):
                x["status"], x["prompt_ready"] = "awaiting_approval", True
            mstage.side_effect = stage

            flow._active[fl["office_id"]] = fl["id"]
            result = {}
            th = threading.Thread(target=lambda: result.__setitem__("ok", flow._run_department(fl, dep, {})))
            th.start()

            time.sleep(0.5)   # 승인 전: runs.start 는 아직 안 불렸어야 한다
            self.assertEqual(started["count"], 0)
            self.assertEqual(dep["desks"][0]["status"], "awaiting_approval")

            flow.approve_desk(fl["id"], "d_dept:0")   # 승인 → 전송
            th.join(timeout=5)
            self.assertFalse(th.is_alive())
            self.assertEqual(started["count"], 1)
            self.assertEqual(dep["desks"][0]["status"], "ok")
            self.assertTrue(result.get("ok"))
            flow._clear_approvals(fl["id"])
            flow._active.pop(fl["office_id"], None)

    def test_reject_marks_failed_no_start(self):
        fl, dep = self._flow_and_dep()
        started = {"count": 0}

        with patch.object(flow, "_flows_dir") as mflow, \
             patch.object(flow, "_save"), \
             patch.object(flow.agent_store, "get", return_value={"id": "a_1"}), \
             patch.object(flow.runs, "find_task", return_value={"id": "t", "name": "요약"}), \
             patch.object(flow, "_desk_inputs", return_value=({}, [], 0)), \
             patch.object(flow, "_stage_for_approval") as mstage, \
             patch.object(flow.runs, "start", side_effect=lambda *a: started.__setitem__("count", started["count"] + 1)):
            mflow.return_value = Path(tempfile.mkdtemp())

            def stage(fl_, x, a, t, v):
                x["status"], x["prompt_ready"] = "awaiting_approval", True
            mstage.side_effect = stage

            flow._active[fl["office_id"]] = fl["id"]
            result = {}
            th = threading.Thread(target=lambda: result.__setitem__("ok", flow._run_department(fl, dep, {})))
            th.start()
            time.sleep(0.5)
            flow.reject_desk(fl["id"], "d_dept:0")   # 거부 → 실패, 전송 안 함
            th.join(timeout=5)
            self.assertFalse(th.is_alive())
            self.assertEqual(started["count"], 0)
            self.assertEqual(dep["desks"][0]["status"], "failed")
            self.assertFalse(result.get("ok"))
            flow._clear_approvals(fl["id"])
            flow._active.pop(fl["office_id"], None)


if __name__ == "__main__":
    unittest.main()
