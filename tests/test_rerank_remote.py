"""검색 순위 매기기(리랭커)를 젯슨 /rerank 로 보내기(2026-09-29) — 점수 척도가 PC 계산과 같고, 실패하면 CPU 로 넘어가지 않는다."""

import io
import json
import unittest
from unittest import mock

from app.rag import embedder, search

# PC(CPU, sentence-transformers 6.1)에서 잰 기준: 원점수 → 지금 관련도(sigmoid 두 번)
REF = [(4.0999, 0.7278), (-11.0300, 0.5000), (-0.7452, 0.5798)]


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RemoteRerankTest(unittest.TestCase):
    rows = {1: {"text": "a"}, 2: {"text": "b"}, 3: {"text": "c"}}

    def dev(self, rerank=True):
        return {"url": "http://jetson:8790", "token": "t", "rerank": rerank}

    def test_remote_scores_match_pc_scale(self):
        body = json.dumps({"model": search.RERANK_MODEL, "logits": [x for x, _ in REF]}).encode()
        with mock.patch.object(embedder, "device_config", return_value=self.dev()), \
             mock.patch("urllib.request.urlopen", return_value=_Resp(body)), \
             mock.patch.object(search, "_get_reranker", side_effect=AssertionError("PC 리랭커를 부르면 안 됨")):
            got = dict(search.rerank("q", [1, 2, 3], self.rows))
        for cid, (_, want) in zip((1, 2, 3), REF):
            self.assertAlmostEqual(got[cid], want, places=3)

    def test_remote_failure_does_not_fall_back_to_cpu(self):
        with mock.patch.object(embedder, "device_config", return_value=self.dev()), \
             mock.patch("urllib.request.urlopen", side_effect=OSError("refused")), \
             mock.patch("time.sleep"), \
             mock.patch.object(search, "_get_reranker", side_effect=AssertionError("PC 리랭커를 부르면 안 됨")):
            with self.assertRaises(RuntimeError):
                search.rerank("q", [1, 2, 3], self.rows)

    def test_switch_off_keeps_pc_path(self):
        class Fake:
            def predict(self, pairs, **k):
                return [0.983696, 0.000016, 0.321865]   # 확률(CrossEncoder 기본 Sigmoid 후)
        with mock.patch.object(embedder, "device_config", return_value=self.dev(rerank=False)), \
             mock.patch.object(search, "_get_reranker", return_value=Fake()):
            got = dict(search.rerank("q", [1, 2, 3], self.rows))
        for cid, (_, want) in zip((1, 2, 3), REF):
            self.assertAlmostEqual(got[cid], want, places=3)


if __name__ == "__main__":
    unittest.main()
