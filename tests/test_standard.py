"""연구팀 공통 표준 층(표준 v1.1, 2026-09-26) 시험 — 실제 모델은 부르지 않는다.

  - 공통 규칙 파일 한 곳을 고치면 적용 대상 직원 모두의 시스템 프롬프트(가짜 모델이 받는 것)에 반영되는지
  - 기자 훅 4개에서 날짜 일관성·확신도 상한 중복 코드가 빠졌고, 공통 검증이 같은 규칙을 지키는지
  - 확인 여부를 실행 기록과 대조: 앵커는 거부, 보강 근거는 경고
  - 금지 구절·검색 결과 번호, 훅에 기준값 전달, 실행 기록의 표준 버전·경고
"""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# 다른 시험 파일과 같은 방식: 데이터 폴더는 임시 폴더(실데이터를 건드리지 않게). 표준 설정·훅·직원 파일은 저장소 경로에서 명시적으로 읽는다.
if "AGENT_TOWN_DATA" not in os.environ:
    _sandbox = tempfile.TemporaryDirectory(prefix="agent-town-std-")
    os.environ["AGENT_TOWN_DATA"] = _sandbox.name
os.environ.setdefault("AGENT_TOWN_NO_DOTENV", "1")
os.environ.setdefault("LLM_MODE", "mock")

from app.chat import llm, tool_chat          # noqa: E402
from app.runs import service, standard, verify   # noqa: E402
from app.tools import registry               # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REAL_CFG = ROOT / "data/config/standard.json"
REAL_RULES = ROOT / "data/config/common_rules.md"
REPORTERS = {"a_0fdf5aca": "t_b79f85", "a_db99edd1": "t_d3fea4", "a_61ebd0cd": "t_b163d7", "a_06d6feb4": "t_67575c"}
DATE_FIELD = {"a_db99edd1": "issued_at_et"}      # 정책만 공개일 필드 이름이 다르다
GEO = "a_61ebd0cd"
HEAD = "a_3a5a73e3"                               # 표준 적용 대상이 아닌 직원(아직)


def load_agent(aid: str) -> dict:
    d = ROOT / "data/agents" / aid
    a = json.loads((d / "agent.json").read_text(encoding="utf-8"))
    a["persona"] = (d / "persona.md").read_text(encoding="utf-8")
    a["work"] = {"tasks": json.loads((d / "tasks.json").read_text(encoding="utf-8"))["tasks"]}
    a["model"] = json.loads((d / "model.json").read_text(encoding="utf-8"))
    return a


class FakeSession:
    """가짜 모델: 받은 시스템 프롬프트를 남기고 바로 '완료'라고 답한다."""
    seen: list = []

    def __init__(self, model, system, messages, specs, max_tokens=None):
        FakeSession.seen.append(system)

    def step(self, allow_tools=True):
        return tool_chat.Turn("완료")

    def add_results(self, results): pass
    def restrict_tools(self, specs): pass


class StandardLayer(unittest.TestCase):
    def setUp(self):
        for name, value in (("CONFIG_PATH", REAL_CFG), ("RULES_PATH", REAL_RULES)):
            p = patch.object(standard, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_common_layer_keeps_the_moved_rules_for_every_reporter(self):
        for aid in REPORTERS:
            f = DATE_FIELD.get(aid, "released_at_et")
            data = {"evidence": [{"evidence_id": "D-0001", f: "2026-09-26", "date_confirmed": False},     # 날짜 추측
                                 {"evidence_id": "D-0002", f: None, "date_confirmed": True}],            # 확인했다며 날짜 없음
                    "signals": [{"signal_id": "S-001", "evidence_ids": ["D-0001"], "confidence": 0.7}]}  # 확인 근거 없이 0.7
            errs, _ = standard.check(data, "", None, aid)
            self.assertTrue(any("D-0001" in e and "date_confirmed=false" in e and f in e for e in errs), aid)
            self.assertTrue(any("D-0002" in e and "비어 있습니다" in e for e in errs), aid)
            self.assertTrue(any("S-001" in e and "0.4 이하" in e for e in errs), aid)
        self.assertEqual(standard.check({"evidence": [{"evidence_id": "D-1", "released_at_et": "x", "date_confirmed": False}]}, "", None, HEAD), ([], []))

    # ------------------------------------------------------------ 확인 여부를 실행 기록과 대조
    def test_confirmed_flag_checked_against_run_trace(self):
        fr_url = "https://www.federalregister.gov/documents/2026/09/24/2026-19537/measures-to-restrict"
        sources = [
            {"via": "tool", "tool": "federal_register_geo", "ok": True, "args": {}, "items": [{"url": fr_url, "date": "2026-09-24"}]},
            {"via": "tool", "tool": "page_fetch", "ok": True, "args": {"url": "https://pv.example.com/a?utm=1"},
             "items": [{"url": "https://www.pv.example.com/a/", "date": "2026-09-25"}]},
            {"via": "tool", "tool": "web_search", "ok": True, "args": {}, "items": [{"url": "https://www.reuters.com/x", "date": "2026-09-26"}]},
        ]
        ev = [
            {"evidence_id": "D-0001", "is_anchor": True, "url": fr_url, "released_at_et": "2026-09-24", "date_confirmed": True},
            {"evidence_id": "D-0002", "is_anchor": True, "doc_id": "FR-2026-19537", "url": "https://federalregister.gov/d/2026-19537",
             "released_at_et": "2026-09-24", "date_confirmed": True},                                           # 문서번호로 맞춤
            {"evidence_id": "D-0003", "is_anchor": True, "url": "https://www.federalregister.gov/documents/2026/09/24/2026-00000/other-doc",
             "released_at_et": "2026-09-26", "date_confirmed": True},   # 1차 도메인이라 앵커 자격은 통과 — 실행 기록에 없다는 것만 걸린다
            {"evidence_id": "D-0004", "is_anchor": False, "url": "https://www.reuters.com/x", "released_at_et": "2026-09-26", "date_confirmed": True},
            {"evidence_id": "D-0005", "is_anchor": False, "url": "https://pv.example.com/a", "released_at_et": "2026-09-25", "date_confirmed": True},
        ]
        errs, warns = standard.check({"evidence": ev, "signals": []}, "", sources, GEO)
        self.assertEqual([e for e in errs if "D-0001" in e or "D-0002" in e or "D-0005" in e], [])   # 실제로 연 근거는 통과
        self.assertTrue(any("D-0003" in e and "앵커" in e for e in errs))       # 검색 결과 날짜만 본 앵커 → 거부
        self.assertTrue(any("D-0004" in w for w in warns))                        # 보강 근거 → 경고만
        self.assertFalse(any("D-0004" in e for e in errs))
        self.assertEqual(standard.check({"evidence": ev, "signals": []}, "", None, GEO), ([], []))     # 실행 기록 없으면(검증 시험) 대조 생략

    def test_fred_series_confirmed_by_series_id(self):
        sources = [{"via": "tool", "tool": "fred_series", "ok": True, "args": {"series_id": "dgs10"}, "items": []}]
        ev = [{"evidence_id": "D-0001", "series_id": "DGS10", "url": "https://fred.stlouisfed.org/series/DGS10",
               "released_at_et": "2026-09-25", "date_confirmed": True}]
        errs, warns = standard.check({"evidence": ev}, "", sources, "a_0fdf5aca")
        self.assertEqual((errs, warns), ([], []))

    # ------------------------------------------------------------ 금지 구절·검색 결과 번호
    def test_forbidden_phrases_and_footnotes(self):
        data = {"summary_ko": "목표주가 상향 여지", "notes": {"a": "임원 매수 공시(사건)", "b": "중국산 수입 비중 확대"},
                "evidence_sentence": "tariff effective 2026-10-01 [W4]"}
        text = "## A. 요약\n매수 추천 [W3] 문장\n```json\n" + json.dumps(data, ensure_ascii=False) + "\n```"
        errs, warns = standard.check(data, text, None, GEO)
        self.assertTrue(any("목표주가" in e for e in errs))
        self.assertFalse(any("임원 매수" in e or "비중 확대" in e for e in errs))    # 사건 이름·정상 문장은 통과
        self.assertTrue(any("[W4]" in e for e in errs))                               # JSON 안의 번호는 오류
        self.assertTrue(any("매수 추천" in w for w in warns))                         # 글 부분은 경고
        cleaned, n = standard.strip_prose_footnotes(text)
        self.assertEqual(n, 1)
        self.assertNotIn("[W3]", cleaned)
        self.assertIn("[W4]", cleaned)                                                 # JSON 안은 건드리지 않는다

    # ------------------------------------------------------------ 훅에 기준값 전달(격리 실행)
    def test_anchor_kind_classifies_primary_wire_and_none(self):
        """표준 v1.2: 앵커 등급 — 1차 자료=primary, 통신사(도메인만)=wire_last_resort, 그 밖=none."""
        self.assertEqual(standard.anchor_kind({"url": "https://www.federalregister.gov/documents/x", "source": "Federal Register"}), "primary")
        self.assertEqual(standard.anchor_kind({"url": "https://www.reuters.com/x", "source": "Reuters"}), "wire_last_resort")
        self.assertEqual(standard.anchor_kind({"url": "https://www.vinetur.com/x", "source": "Vinetur"}), "none")

    def test_wire_last_resort_anchor_needs_date_source_and_caps_signal(self):
        """통신사를 최후 수단 앵커로 쓰려면 date_source=search_metadata 가 있어야 하고, 그 앵커에 기대는 신호는 강도·확신도 상한이 걸린다."""
        data = {"evidence": [
            {"evidence_id": "D-0001", "is_anchor": True, "source": "Vinetur", "url": "https://www.vinetur.com/x",
             "released_at_et": None, "date_confirmed": False},
            {"evidence_id": "D-0002", "is_anchor": True, "source": "Reuters", "url": "https://www.reuters.com/x",
             "released_at_et": None, "date_confirmed": False},                              # date_source 없이 통신사 앵커 시도
            {"evidence_id": "D-0003", "is_anchor": True, "source": "Reuters", "url": "https://www.reuters.com/y",
             "released_at_et": "2026-09-20", "date_confirmed": False, "date_source": "search_metadata"},   # 올바른 최후 수단
        ], "signals": [
            {"signal_id": "GEO-001", "evidence_ids": ["D-0002"], "magnitude": 2, "confidence": 0.7, "current_state": {"still_valid": True}},
            {"signal_id": "GEO-002", "evidence_ids": ["D-0003"], "magnitude": 2, "confidence": 0.4, "current_state": {"still_valid": True}},   # 상한 초과(magnitude)
            {"signal_id": "GEO-003", "evidence_ids": ["D-0003"], "magnitude": 1, "confidence": 0.4, "current_state": {"still_valid": True}},   # 상한 이내
        ]}
        errs, warns = standard.check(data, "", None, GEO)
        self.assertTrue(any("D-0001" in e and "허용 앵커 출처가 아닙니다" in e for e in errs))
        self.assertTrue(any("D-0002" in e and "1차 원문" in e for e in errs))                # date_source 없이 통신사 앵커 → 거부
        self.assertFalse(any("D-0003" in e and ("허용 앵커" in e or "1차 원문" in e) for e in errs))   # D-0003 은 올바른 최후 수단
        self.assertTrue(any("GEO-002" in e and "magnitude=2" in e for e in errs))             # 통신사 최후수단 앵커 → 강도 상한(1) 위반
        self.assertFalse(any("GEO-003" in e for e in errs))                                  # 상한 이내라 통과

    def test_staleness_warns_but_never_rejects_and_respects_cadence(self):
        """표준 v1.2.1: source_date 는 더는 거부하지 않는다. 자료 주기는 신호가 인용한 근거(FRED frequency_short·
        Census 월간 고정·관보 등 제외)로 정하고, 허용 지연을 넘으면 [stale] 경고만."""
        sig = lambda sd, eids=("D-0001",), valid=True: {
            "signal_id": "X-001", "evidence_ids": list(eids),
            "current_state": {"still_valid": valid, "source_date": sd}}
        ev_fred_weekly = {"evidence_id": "D-0001", "series_id": "S-W", "url": "https://fred.stlouisfed.org/series/S-W"}
        ev_fred_daily = {"evidence_id": "D-0002", "series_id": "S-D", "url": "https://fred.stlouisfed.org/series/S-D"}
        ev_census = {"evidence_id": "D-0003", "url": "https://www.census.gov/foreign-trade/x"}
        ev_fedreg = {"evidence_id": "D-0004", "url": "https://www.federalregister.gov/documents/2026/09/01/x"}
        ev_unknown = {"evidence_id": "D-0005", "url": "https://example.com/x"}
        window = {"start": "2026-09-19", "end": "2026-09-26"}

        with patch.object(standard, "fred_frequency_short", lambda sid: {"S-W": "W", "S-D": "D"}.get(sid)):
            # FRED 주간(W) 시리즈 근거 — 허용 10일: 5일 늦음은 통과, 15일 늦음은 stale 경고(오류 아님)
            data = {"window": window, "evidence": [ev_fred_weekly], "signals": [sig("2026-09-21")]}
            errs, warns = standard.check(data, "", None, GEO)
            self.assertEqual(errs, []); self.assertEqual(warns, [])
            data2 = {"window": window, "evidence": [ev_fred_weekly], "signals": [sig("2026-09-11")]}
            errs, warns = standard.check(data2, "", None, GEO)
            self.assertEqual(errs, [])
            self.assertTrue(any("[stale]" in w and "weekly" in w for w in warns))
            # FRED 일간(D) 시리즈 근거 — 허용 3영업일: still_valid=False 는 자료 주기와 무관하게 항상 오류
            daily = {"window": window, "evidence": [ev_fred_daily],
                     "signals": [sig("2026-09-20", eids=("D-0002",), valid=False)]}
            errs, warns = standard.check(daily, "", None, GEO)
            self.assertTrue(any("still_valid" in e for e in errs))
            # Census 는 series_id 없이도 월간 고정(허용 45일) — 20일 늦음은 통과
            census_data = {"window": window, "evidence": [ev_census],
                            "signals": [sig("2026-09-06", eids=("D-0003",))]}
            self.assertEqual(standard.check(census_data, "", None, GEO), ([], []))
            # 관보(federalregister.gov) 근거만 인용 — 시계열이 아니므로 신선도 검사 자체를 건너뛴다(아주 오래돼도 경고 없음)
            fedreg_data = {"window": window, "evidence": [ev_fedreg],
                           "signals": [sig("2020-01-01", eids=("D-0004",))]}
            self.assertEqual(standard.check(fedreg_data, "", None, GEO), ([], []))
            # 주기를 모르는 근거(FRED 도, Census 도 아님) — 월간(45일) 기본값으로 처리: 20일 늦음은 통과
            unknown_data = {"window": window, "evidence": [ev_unknown],
                            "signals": [sig("2026-09-06", eids=("D-0005",))]}
            self.assertEqual(standard.check(unknown_data, "", None, GEO), ([], []))
            # 표준 v1.2 이전 규칙(관측 종료일보다 이르면 무조건 거부)은 사라졌다 — 허용 범위 안이면 경고도 없다
            tight = {"window": window, "evidence": [ev_fred_weekly], "signals": [sig("2026-09-24")]}
            self.assertEqual(standard.check(tight, "", None, GEO), ([], []))

    def test_fred_frequency_lookup_is_cached_locally(self):
        """같은 series_id 는 한 번만 조회하고 로컬 캐시(JSON)에 남긴다 — 두 번째 호출은 네트워크를 타지 않는다."""
        calls = []

        class _FakeResp:
            def __enter__(self_inner):
                return self_inner
            def __exit__(self_inner, *a):
                return False
            def read(self_inner):
                return json.dumps({"seriess": [{"frequency_short": "M"}]}).encode()

        def _fake_urlopen(url, timeout=8):
            calls.append(url)
            return _FakeResp()

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "fred_frequency_cache.json"
            with patch.object(standard, "FRED_FREQUENCY_CACHE_PATH", cache_path), \
                 patch.object(standard.os, "getenv", lambda k, *a: "FAKEKEY" if k == "FRED_API_KEY" else None), \
                 patch.object(standard.urllib.request, "urlopen", _fake_urlopen):
                freq1 = standard.fred_frequency_short("CPIAUCSL")
                freq2 = standard.fred_frequency_short("CPIAUCSL")
            self.assertEqual(freq1, "M"); self.assertEqual(freq2, "M")
            self.assertEqual(len(calls), 1)                 # 두 번째 호출은 캐시에서 읽어 network call 없음
            self.assertIn("CPIAUCSL", json.loads(cache_path.read_text(encoding="utf-8")))

    def test_run_record_keeps_standard_version_warnings_and_cleans_prose(self):
        run = {"id": "r_std00001", "agent_id": GEO, "task_id": "t_x", "inputs": {}, "attempts": [], "sources": [],
               "notice": None, "status": "running", "usage": {}}
        with patch.object(service, "_augment_answer", side_effect=lambda x: x):
            service._check_and_status({}, run, "요약 [W3] 끝\n```json\n{\"a\": 1}\n```", {"ok": True, "problems": [], "warnings": ["w-공통"]})
        self.assertNotIn("[W3]", run["answer"])
        self.assertIn("w-공통", run["check"]["warnings"])
        self.assertTrue(any("1개를 지웠습니다" in w for w in run["check"]["warnings"]))
        v1 = standard.version_info()
        self.assertEqual(v1["version"], "std-1.2.1")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "common_rules.md"
            p.write_text(REAL_RULES.read_text(encoding="utf-8") + "\n- 한 줄 추가", encoding="utf-8")
            with patch.object(standard, "RULES_PATH", p):
                self.assertNotEqual(standard.version_info()["fingerprint"], v1["fingerprint"])   # 버전 이름이 같아도 파일이 바뀌면 지문이 바뀐다

    def test_execute_records_standard_version_for_members(self):
        agent_dir = Path(os.environ["AGENT_TOWN_DATA"]) / "agents" / GEO
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "agent.json").write_text("{}")
        run = {"id": "r_std00002", "agent_id": GEO, "task_id": "t_x", "inputs": {}, "attempts": [], "sources": [],
               "notice": None, "status": "running", "usage": {}}
        task = {"id": "t_x", "name": "시험", "instruction": "x", "inputs": [], "retries": 0}
        from app.chat import pipeline
        with patch.object(pipeline, "answer", return_value={"answer": "{}", "sources": [], "notice": None, "history": None}), \
             patch.object(service, "_verify_execution", return_value={"ok": True, "problems": [], "kinds": [], "warnings": []}), \
             patch.object(service, "_save"), patch.object(service, "_augment_answer", side_effect=lambda x: x):
            service._execute({"id": GEO}, task, run, {})
        self.assertEqual(run["standard"]["version"], "std-1.2.1")
        self.assertEqual(len(run["standard"]["fingerprint"]), 12)




class ToolResultTrim(unittest.TestCase):
    """③ 도구 결과 줄이기(2026-09-26): 인용에 쓰는 항목은 남기고, 같은 실행의 중복은 표지로, 상한에서 결과를 잃지 않게."""

    def _fr(self, num, title="Measures To Restrict Stockpiling of Polysilicon", eff="2026-10-01"):
        return {"title": title, "type": "Rule", "publication_date": "2026-09-24", "effective_on": eff, "document_number": num,
                "agency_names": ["Commerce Department", "Industry and Security Bureau"],
                "html_url": f"https://www.federalregister.gov/documents/2026/09/24/{num}/measures-to-restrict-stockpiling-of-polysilicon-and-its-derivatives",
                "abstract": "The Bureau of Industry and Security amends the Export Administration Regulations to impose license requirements on polysilicon " * 3}


class ErrorCodesAndRetryPolicy(unittest.TestCase):
    """① 3-b: 오류 분류 코드와 재시도 정책(2026-09-26 결정). 실제 모델 호출 없음."""

    def setUp(self):
        for name, value in (("CONFIG_PATH", REAL_CFG), ("RULES_PATH", REAL_RULES)):
            p = patch.object(standard, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _run(self, verdicts):
        """가짜 모델·가짜 검증으로 실행 하나. verdicts: 검증이 차례로 돌려줄 문제 목록들."""
        from app.chat import pipeline
        agent_dir = Path(os.environ["AGENT_TOWN_DATA"]) / "agents" / GEO
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "agent.json").write_text("{}")
        run = {"id": "r_pol00001", "agent_id": GEO, "task_id": "t_x", "inputs": {}, "attempts": [], "sources": [],
               "notice": None, "status": "running", "usage": {}}
        calls, fix_msgs, seq = {"n": 0}, [], iter(verdicts)

        def fake_answer(agent_id, agent, messages, **kw):
            calls["n"] += 1
            if calls["n"] > 1:
                fix_msgs.append(messages[-1]["content"])
            return {"answer": "요약\n```json\n{\"a\": 1}\n```", "sources": [], "notice": None, "history": None}

        def fake_verify(task, text, agent_id, run):
            probs = next(seq)
            return {"ok": not probs, "problems": probs, "kinds": ["content"] if probs else [], "warnings": []}
        task = {"id": "t_x", "name": "시험", "instruction": "x", "inputs": [], "retries": 2}
        with patch.object(pipeline, "answer", side_effect=fake_answer), patch.object(service, "_verify_execution", side_effect=fake_verify), \
             patch.object(service, "_save"), patch.object(service, "_augment_answer", side_effect=lambda x: x):
            service._execute({"id": GEO}, task, run, {})
        return run, calls["n"] - 1, fix_msgs

    def test_format_errors_get_json_only_retry(self):
        run, retries, _ = self._run([["[format] 커버리지 누락: ['US-CN']"], []])
        self.assertEqual((retries, run["status"]), (1, "ok"))

    def test_data_missing_passes_as_hold_without_retry(self):
        run, retries, _ = self._run([["[data_missing] GEO-001: magnitude 2 인데 market_reaction 이 비어 있습니다."]])
        self.assertEqual(retries, 0)
        self.assertEqual(run["status"], "ok")
        self.assertIn("보류", run["stage"])
        self.assertEqual(len(run["hold"]), 1)
        from app.offices import flow
        desk = {}
        out = flow._with_hold({"signals": []}, run, desk)                         # 다음 단계에 보류 표시
        self.assertEqual(out["_hold"]["reason"], "data_missing")
        self.assertEqual(desk["hold"], run["hold"])
        self.assertEqual(flow._with_hold({"signals": []}, {"status": "ok"}, {}), {"signals": []})

    def test_tool_failure_fails_run_without_retry(self):
        run, retries, _ = self._run([["[tool_failure] 판단 불가: 필수 도구 'web_search' 호출이 모두 실패했습니다."]])
        self.assertEqual((retries, run["status"]), (0, "error"))
        self.assertIn("도구 실패", run["stage"])

    def test_unclassified_stops_and_records_without_retry(self):
        run, retries, _ = self._run([["[legacy_unclassified] 알 수 없는 오류"]])
        self.assertEqual((retries, run["status"]), (0, "check_failed"))
        self.assertIn("재요청 없이 멈추고 기록", run["stage"])
        self.assertEqual(run["check"]["problems"], ["[legacy_unclassified] 알 수 없는 오류"])

    def test_mixed_retries_only_fixable_part_and_hides_data_missing_from_model(self):
        dm = "[data_missing] 필수 코리도어 US-CN 는 not_covered_corridors 에 둘 수 없습니다"
        run, retries, fixes = self._run([["[citation_error] GEO-001: evidence_ids 의 D-0009 가 evidence 에 없습니다.", dm], [dm]])
        self.assertEqual(retries, 1)
        self.assertIn("D-0009", fixes[0])
        self.assertNotIn("not_covered_corridors", fixes[0])                      # 자료 부족은 재요청 문구에 넣지 않는다(지어내기 방지)
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["hold"], [dm])

    def test_summary_line_of_long_problem_lists_does_not_stop_retries(self):
        many = [f"[format] 커버리지 누락 {i}" for i in range(20)] + ["… 외 5건"]
        self.assertTrue(service._retryable({"ok": False, "kinds": ["content"], "problems": many, "codes": ["format"]}, True))
        self.assertTrue(service._retryable({"ok": False, "kinds": ["content"], "problems": many}, True))   # codes 가 없어도 요약 줄은 분류에서 뺀다

    def test_platform_problems_get_codes(self):
        self.assertEqual(standard.platform_code("판단 불가: 필수 도구 'x' 호출이 모두 실패했습니다."), "tool_failure")
        self.assertEqual(standard.platform_code("판단 불가: 필수 전문자료에서 사용할 근거를 얻지 못했습니다."), "data_missing")

    def test_transient_tool_errors_retry_twice_then_give_up(self):
        import urllib.error
        from app.tools import runner
        tool = registry.NAVER_NEWS

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n): return b'{"results": []}'
        seq = {"n": 0}

        def flaky(req, timeout=None):
            seq["n"] += 1
            if seq["n"] <= 2:
                raise urllib.error.URLError("connection reset")
            return Resp()
        with patch.object(runner, "TOOL_RETRY_WAIT", 0), patch("urllib.request.urlopen", side_effect=flaky):
            self.assertEqual(runner._request_with_retry(object(), tool), b'{"results": []}')
        self.assertEqual(seq["n"], 3)                                             # 처음 1회 + 재시도 2회
        seq["n"] = 0

        def forbidden(req, timeout=None):
            seq["n"] += 1
            raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        with patch.object(runner, "TOOL_RETRY_WAIT", 0), patch("urllib.request.urlopen", side_effect=forbidden):
            with self.assertRaises(runner._ToolHttpError):
                runner._request_with_retry(object(), tool)
        self.assertEqual(seq["n"], 1)                                             # 권한 없음은 다시 해도 같다 → 재시도 없음
        seq["n"] = 0

        def down(req, timeout=None):
            seq["n"] += 1
            raise urllib.error.URLError("down")
        with patch.object(runner, "TOOL_RETRY_WAIT", 0), patch("urllib.request.urlopen", side_effect=down):
            with self.assertRaises(runner._ToolHttpError) as cm:
                runner._request_with_retry(object(), tool)
        self.assertEqual(seq["n"], 3)
        self.assertIn("3번 시도했지만 실패", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
