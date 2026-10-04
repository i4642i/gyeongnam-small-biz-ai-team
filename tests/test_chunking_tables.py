"""공시 표 조각 나누기(2026-09-28) — 큰 표를 나눠도 조각마다 열 머리(날짜)·단위가 남는가."""

import unittest

from app.rag import chunking
from app.rag.extract import Page

FRONT = "| 항목 | 값 |\n|---|---|\n| 티커 | AEE |\n| 문서 종류 | 10-Q |\n| 기간 | 2026-06 |\n\n---\n\n"


def _table(n_rows: int) -> str:
    head = ("CONSOLIDATED BALANCE SHEET\n(Unaudited) (In millions)\n"
            "|  | | | | |\n| --- | --- | --- | --- | --- |\n"
            "|  | | June 30, 2026 | | December 31, 2025 |\n")
    rows = "".join(f"| Line item number {i} with a fairly long label | | {1000 + i:,} | | {900 + i:,} |\n"
                   for i in range(n_rows))
    return head + rows


class TableChunkTest(unittest.TestCase):
    def chunks(self, body: str) -> list[str]:
        return [t for _, t in chunking.make_chunks([Page(None, FRONT + body)])]

    def test_front_matter_without_headings_uses_structured_path(self):
        out = self.chunks(_table(3))
        self.assertTrue(all(t.startswith("[AEE · 10-Q · 2026-06") for t in out))

    def test_small_table_is_one_chunk(self):
        out = [t for t in self.chunks(_table(3)) if "Line item" in t]
        self.assertEqual(len(out), 1)

    def test_large_table_repeats_header_and_units(self):
        out = [t for t in self.chunks(_table(60)) if "Line item" in t]
        self.assertGreater(len(out), 1)
        for t in out:
            self.assertIn("June 30, 2026", t)
            self.assertIn("(In millions)", t)
        joined = "\n".join(out)
        for i in range(60):   # 행이 빠지지 않는다
            self.assertIn(f"Line item number {i} ", joined)

    def test_continued_table_inherits_header(self):
        cont = "".join(f"| Continued row {i} | | {50 + i} | | {40 + i} |\n" for i in range(3))
        out = self.chunks(_table(3) + "\n" + cont)
        tail = [t for t in out if "Continued row" in t][0]
        self.assertIn("앞 표의 머리를 이어받음", tail)
        self.assertIn("June 30, 2026", tail)

    def test_year_only_header_row_is_header_not_data(self):
        body = "|  | | 2026 | | 2025 |\n| --- | --- | --- | --- | --- |\n| Revenue | | 794.8 | | 735.0 |\n"
        t = [t for t in self.chunks(body) if "Revenue" in t][0]
        self.assertLess(t.index("2026"), t.index("Revenue"))


if __name__ == "__main__":
    unittest.main()
