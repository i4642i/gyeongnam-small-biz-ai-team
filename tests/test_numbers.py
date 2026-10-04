"""숫자 변수 방식(app/runs/numbers.py, 2026-09-28) — 이번 실행에서 실제로 난 오류 유형을 잡는가."""

import json
import unittest
from unittest import mock

from app.runs import numbers, soft

PROSE = ("[PFS · 10-Q · 2026-06 · MD&A]\nNet interest income for the three months ended June 30, 2026 was $202.7 million, "
         "compared to $187.1 million for the three months ended June 30, 2025.")
TABLE = ("[PFS · 10-Q · 2026-06 · (개요)]\nCONSOLIDATED STATEMENTS OF INCOME\n(Unaudited) (Dollars in thousands)\n"
         "| --- | --- | --- | --- | --- |\n|  | | Three Months Ended June 30, 2026 | | Three Months Ended June 30, 2025 |\n"
         "| Interest on deposits and borrowings | | 226,892 | | 210,004 |")
BAL = ("[AEE · 10-Q · 2026-06 · (개요)]\nCONSOLIDATED BALANCE SHEET\n(Unaudited) (In millions)\n"
       "| --- | --- | --- | --- | --- |\n|  | | | June 30, 2026 | | December 31, 2025 |\n"
       "| Long-term Debt, Net (includes $414 and $426 related to VIEs, respectively) | | | 19,064 | | 18,214 |")


def _answer(variables, finding, evidence, direction=None, verdict="supported"):
    check = {"verify_point": "Compare net interest income", "finding": finding, "verdict": verdict,
             "confidence": 0.8, "evidence_ids": [e["evidence_id"] for e in evidence]}
    if direction:
        check["direction"] = direction
    first = "{" + variables[0]["id"] + "}"
    data = {"ticker": "PFS", "checks": [check], "evidence": evidence, "variables": variables, "summary_ko": "요약 " + first}
    return "## A. 요약\n순이자이익 " + first + "\n\n```json\n" + json.dumps(data, ensure_ascii=False) + "\n```\n"


def _var(id_, raw, label="net interest income", scale="million", ptype="3M", end="2026-06-30", ev="E-001", unit="USD"):
    return {"id": id_, "label": label, "raw": raw, "unit": unit, "scale": scale, "period_type": ptype,
            "period_end": end, "scope": "consolidated", "source": "evidence", "evidence_id": ev}


class NumbersTest(unittest.TestCase):
    def run_with(self, chunks, text):
        with mock.patch.object(numbers, "_load_chunks", return_value=chunks):
            return numbers.problems(text, {}), numbers.apply(text, {})

    def test_correct_prose_passes_and_fills(self):
        ev = [{"evidence_id": "E-001", "quote": "Net interest income for the three months ended June 30, 2026 was $202.7 million"}]
        text = _answer([_var("NII", "202.7"), _var("NII_PY", "187.1", end="2025-06-30")],
                       "순이자이익은 {NII}로 전년 동기 {NII_PY} 대비 {chg:NII,NII_PY}했다.", ev,
                       direction={"expected": "up", "from": "NII_PY", "to": "NII"})
        errs, (out, _) = self.run_with([PROSE], text)
        self.assertEqual(errs, [])
        self.assertIn("2억 270만 달러로 전년 동기 1억 8,710만 달러 대비 1,560만 달러(8.3%) 증가했다", out)
        self.assertIn("순이자이익 2억 270만 달러", out)          # A 절도 채운다
        self.assertNotIn("{NII", out)

    def test_wrong_row_is_caught(self):   # PFS 실제 오류: 이자비용 줄을 순이자이익으로
        ev = [{"evidence_id": "E-001", "quote": "Interest on deposits and borrowings | | 226,892"}]
        text = _answer([_var("NII", "226,892", scale="thousand")], "순이자이익은 {NII}이다.", ev)
        errs, (out, warns) = self.run_with([TABLE], text)
        self.assertTrue(any("지표 이름" in e for e in errs), errs)
        self.assertTrue(all(e.startswith(numbers.TAG) for e in errs))
        self.assertIn(numbers.UNVERIFIED, out)   # 재요청 뒤에도 남으면 〔확인 안 됨〕
        self.assertTrue(any("확인하지 못한 변수" in w for w in warns))

    def test_value_not_in_chunk(self):
        ev = [{"evidence_id": "E-001", "quote": "Net interest income for the three months ended June 30, 2026 was $202.7 million"}]
        text = _answer([_var("NII", "226.8")], "순이자이익은 {NII}이다.", ev)
        errs, _ = self.run_with([PROSE], text)
        self.assertTrue(any("원문 조각에 없습니다" in e for e in errs), errs)

    def test_period_conflict_in_table(self):
        ev = [{"evidence_id": "E-001", "quote": "Long-term Debt, Net (includes $414 and $426 related to VIEs, respectively)"}]
        text = _answer([_var("LTD", "18,214", label="long-term debt", ptype="instant", end="2026-06-30")], "장기차입금 {LTD}", ev)
        errs, _ = self.run_with([BAL], text)
        self.assertTrue(any("기간" in e for e in errs), errs)

    def test_period_and_unit_ok_in_table(self):
        ev = [{"evidence_id": "E-001", "quote": "Long-term Debt, Net (includes $414 and $426 related to VIEs, respectively)"}]
        text = _answer([_var("LTD", "19,064", label="long-term debt", ptype="instant", end="2026-06-30")], "장기차입금 {LTD}", ev)
        errs, (out, warns) = self.run_with([BAL], text)
        self.assertEqual(errs, [])
        self.assertIn("190억 6,400만 달러", out)
        self.assertFalse(any("기간" in w or "단위" in w for w in warns), warns)

    def test_scale_conflict_in_table(self):
        ev = [{"evidence_id": "E-001", "quote": "Long-term Debt, Net (includes $414 and $426 related to VIEs, respectively)"}]
        text = _answer([_var("LTD", "19,064", label="long-term debt", scale="thousand", ptype="instant", end="2026-06-30")], "장기차입금 {LTD}", ev)
        errs, _ = self.run_with([BAL], text)
        self.assertTrue(any("단위" in e for e in errs), errs)

    def test_typed_number_in_finding_is_caught(self):
        ev = [{"evidence_id": "E-001", "quote": "Net interest income for the three months ended June 30, 2026 was $202.7 million"}]
        text = _answer([_var("NII", "202.7")], "순이자이익은 2억 2,680만 달러로 {NII}", ev)
        errs, _ = self.run_with([PROSE], text)
        self.assertTrue(any("숫자를 직접" in e for e in errs), errs)

    def test_direction_contradicts_supported(self):   # ZION 유형: 줄었는데 supported
        ev = [{"evidence_id": "E-001", "quote": "Net interest income for the three months ended June 30, 2026 was $202.7 million"}]
        text = _answer([_var("NII", "202.7"), _var("NII_PY", "187.1", end="2025-06-30")], "{chg:NII,NII_PY}", ev,
                       direction={"expected": "down", "from": "NII_PY", "to": "NII"})
        errs, _ = self.run_with([PROSE], text)
        self.assertTrue(any("실제 방향은 up" in e for e in errs), errs)

    def test_unknown_placeholder(self):
        ev = [{"evidence_id": "E-001", "quote": "Net interest income for the three months ended June 30, 2026 was $202.7 million"}]
        text = _answer([_var("NII", "202.7")], "{NIM}", ev)
        errs, _ = self.run_with([PROSE], text)
        self.assertTrue(any("NIM" in e for e in errs), errs)

    def _two_vars(self, finding="{chg:NII,NII_PY}"):
        ev = [{"evidence_id": "E-001", "quote": "Net interest income for the three months ended June 30, 2026 was $202.7 million"}]
        return _answer([_var("NII", "202.7"), _var("NII_PY", "187.1", end="2025-06-30")], finding, ev)

    def test_broken_placeholder_in_prose_is_repaired_without_retry(self):   # 9/28 AEE: 글에 '{chg:A,B)}' 3개 남음 + 헛도는 재요청 2번
        text = self._two_vars().replace("## A. 요약\n", "## A. 요약\n| 표 | {chg:NII,NII_PY)} | {NII} |\n")
        errs, (out, notes) = self.run_with([PROSE], text)
        self.assertFalse(any("형식이 틀렸습니다" in e for e in errs), errs)   # 글 부분은 재요청하지 않는다
        self.assertIn("| 표 | 1,560만 달러(8.3%) 증가 | 2억 270만 달러 |", out)   # 고쳐 채우고 옆 표기는 그대로 채움
        self.assertNotIn("{", out.split("```json")[0])
        self.assertTrue(any("서버가 고쳐 채웠습니다" in n for n in notes), notes)
        self.assertTrue(any(n.startswith("[숫자 채우기]") and "깨짐 0" in n for n in notes), notes)

    def test_broken_placeholder_in_json_still_retried(self):   # JSON 안은 모델이 고칠 수 있어 재요청한다
        errs, _ = self.run_with([PROSE], self._two_vars("{chg:NII,NII_PY)}"))
        self.assertTrue(any("형식이 틀렸습니다" in e for e in errs), errs)

    def test_broken_placeholder_with_unknown_name_is_not_guessed(self):
        text = self._two_vars().replace("## A. 요약\n", "## A. 요약\n{chg:NII,NOPE)} 그리고 {chg:NII,NII_PY}\n")
        _, (out, notes) = self.run_with([PROSE], text)
        prose = out.split("```json")[0]
        self.assertIn(numbers.UNVERIFIED + " 그리고 1,560만 달러(8.3%) 증가", prose)
        self.assertTrue(any("고칠 수 없는 자리표시자 1개" in n for n in notes), notes)

    def test_no_variables_key_means_not_applied(self):
        text = "```json\n{\"checks\": []}\n```"
        errs, (out, warns) = self.run_with([PROSE], text)
        self.assertEqual((errs, out, warns), ([], text, []))

    def test_late_problems_pass_only_at_final(self):
        ps = [f"[citation_error] {numbers.TAG} X: 값이 없습니다"]
        self.assertFalse(soft.all_soft(ps))            # 재요청은 한다
        self.assertTrue(soft.all_soft(ps, final=True))  # 재요청을 다 쓴 뒤엔 경고 통과
        self.assertFalse(soft.all_soft(ps + ["다른 오류"], final=True))

    def test_korean_money(self):
        self.assertEqual(numbers.korean_money(202_700_000), "2억 270만 달러")
        self.assertEqual(numbers.korean_money(15_600_000), "1,560만 달러")
        self.assertEqual(numbers.korean_money(19_064_000_000), "190억 6,400만 달러")
        self.assertEqual(numbers.korean_money(1_234_000_000_000), "1조 2,340억 달러")


if __name__ == "__main__":
    unittest.main()
