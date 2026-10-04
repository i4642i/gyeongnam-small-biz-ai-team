"""딥시크 공급사 연결(app/chat/providers.py) 오프라인 테스트. 실제 네트워크·유료 호출 없음 — openai SDK를
가짜로 바꿔서 base_url·오류 매핑·usage 집계만 확인한다. 설계: docs/work_orders/설계안_딥시크_활용_2026-09-25.md."""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# 다른 시험 파일과 한 명령으로 같이 돌 때, 이 파일이 먼저 import 되면 app.config.DATA_DIR 가 실제 data/ 로
# 굳어 버린다(그러면 다른 시험이 만드는 캐릭터가 진짜 폴더에 새어 나간다). 그래서 app 을 import 하기 전에
# 반드시 임시 폴더를 잡는다(이 파일 자체는 store 를 안 쓰지만 DATA_DIR 결정에는 참여한다).
if "AGENT_TOWN_DATA" not in os.environ:
    _sandbox = tempfile.TemporaryDirectory(prefix="agent-town-deepseek-")
    os.environ["AGENT_TOWN_DATA"] = _sandbox.name
os.environ.setdefault("AGENT_TOWN_NO_DOTENV", "1")

from app.chat import providers, tool_chat, usage
from app.rag import graph


def _fake_completion(text="답변", finish_reason="stop", prompt_tokens=10, completion_tokens=5):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None), finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


class DeepSeekProviderRegistration(unittest.TestCase):
    def test_registered_in_providers_and_chat_dispatch(self):
        self.assertIn("deepseek", providers.PROVIDERS)
        self.assertIn("deepseek", providers._CHAT)
        self.assertEqual(providers.PROVIDERS["deepseek"]["default_model"], "deepseek-flash")
        self.assertEqual(providers.key_hint("deepseek"), "DEEPSEEK_API_KEY")

    def test_static_models_fallback_when_key_missing(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEEPSEEK_API_KEY", None)
            models, source = providers.list_models("deepseek")
        self.assertEqual(source, "builtin")
        self.assertIn("deepseek-flash", models)
        self.assertIn("deepseek-v4-pro", models)

    def test_graph_settings_accept_deepseek_without_keyerror(self):
        self.assertIn("deepseek", graph.DEFAULT_MODELS)
        self.assertEqual(graph.DEFAULT_MODELS["deepseek"], "deepseek-flash")

    @patch("openai.OpenAI")
    def test_client_has_timeout_and_retries(self, mock_openai_cls):
        """순간 네트워크 끊김이 업무를 통째로 실패시키지 않도록, 클라이언트에 타임아웃 + 자동 재시도가 걸린다."""
        os.environ["DEEPSEEK_API_KEY"] = "fake"
        try:
            providers._deepseek_client()
        finally:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        kw = mock_openai_cls.call_args.kwargs
        self.assertEqual(kw["max_retries"], 3)
        # httpx.Timeout: 연결 15초, 읽기 180초(정상 pro 호출은 살리고 죽은 연결은 빨리 실패)
        self.assertEqual(kw["timeout"].connect, 15.0)
        self.assertEqual(kw["timeout"].read, 180.0)


class DeepSeekChatCall(unittest.TestCase):
    """_deepseek() 가 openai SDK를 base_url=DEEPSEEK_BASE_URL 로 부르는지, 오류를 DeepSeek 이름으로 바꿔
    전달하는지 — 실제 openai.OpenAI 를 가짜로 바꿔서 확인(네트워크 없음)."""

    def setUp(self):
        os.environ["DEEPSEEK_API_KEY"] = "fake-test-key"

    def tearDown(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)

    @patch("openai.OpenAI")
    def test_calls_deepseek_base_url_with_key(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_completion("안녕하세요")
        mock_openai_cls.return_value = mock_client

        text = providers.chat("deepseek", "deepseek-flash", "시스템", [{"role": "user", "content": "hi"}])

        self.assertEqual(text, "안녕하세요")
        client_kwargs = mock_openai_cls.call_args.kwargs
        self.assertEqual(client_kwargs["api_key"], "fake-test-key")
        self.assertEqual(client_kwargs["base_url"], providers.DEEPSEEK_BASE_URL)
        self.assertEqual(client_kwargs["max_retries"], 3)          # 자동 재시도(2026-09-25)
        self.assertIsNotNone(client_kwargs.get("timeout"))         # 명시적 타임아웃
        self.assertEqual(providers.DEEPSEEK_BASE_URL, "https://api.deepseek.com")
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertEqual(call_kwargs["model"], "deepseek-flash")
        self.assertEqual(call_kwargs["messages"][0], {"role": "system", "content": "시스템"})

    @patch("openai.OpenAI")
    def test_usage_is_recorded(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_completion(prompt_tokens=123, completion_tokens=45)
        mock_openai_cls.return_value = mock_client

        box = usage.start()
        providers.chat("deepseek", "deepseek-flash", "sys", [{"role": "user", "content": "hi"}])
        self.assertEqual(box["input_tokens"], 123)
        self.assertEqual(box["output_tokens"], 45)

    @patch("openai.OpenAI")
    def test_auth_error_maps_to_deepseek_message(self, mock_openai_cls):
        import openai

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = openai.AuthenticationError(
            "bad key", response=MagicMock(status_code=401), body=None)
        mock_openai_cls.return_value = mock_client

        with self.assertRaises(providers.ChatError) as cm:
            providers.chat("deepseek", "deepseek-flash", "sys", [{"role": "user", "content": "hi"}])
        self.assertIn("DeepSeek", str(cm.exception))
        self.assertIn("DEEPSEEK_API_KEY", str(cm.exception))

    @patch("openai.OpenAI")
    def test_empty_response_raises_chat_error(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_completion(text="")
        mock_openai_cls.return_value = mock_client

        with self.assertRaises(providers.ChatError):
            providers.chat("deepseek", "deepseek-flash", "sys", [{"role": "user", "content": "hi"}])


def _fake_tool_call_completion(name="rag_search", args='{"query":"실적"}', call_id="call_1"):
    tc = SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(name=name, arguments=args))
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[tc]), finish_reason="tool_calls")],
        usage=SimpleNamespace(prompt_tokens=50, completion_tokens=20),
    )


class DeepSeekToolChatSession(unittest.TestCase):
    """app/chat/tool_chat.py 의 도구 호출 세션(_DeepSeek) — _OpenAI 와 같은 메시지 모양, base_url 만 다르다."""

    def setUp(self):
        os.environ["DEEPSEEK_API_KEY"] = "fake-test-key"

    def tearDown(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)

    def test_registered_in_sessions(self):
        self.assertIn("deepseek", tool_chat._SESSIONS)
        self.assertIs(tool_chat._SESSIONS["deepseek"], tool_chat._DeepSeek)

    @patch("openai.OpenAI")
    def test_step_uses_deepseek_base_url_and_key(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_completion("최종 답변")
        mock_openai_cls.return_value = mock_client

        session = tool_chat._DeepSeek("deepseek-v4-pro", "시스템", [{"role": "user", "content": "hi"}], [])
        turn = session.step()

        self.assertEqual(turn.text, "최종 답변")
        client_kwargs = mock_openai_cls.call_args.kwargs
        self.assertEqual(client_kwargs["api_key"], "fake-test-key")
        self.assertEqual(client_kwargs["base_url"], providers.DEEPSEEK_BASE_URL)
        self.assertEqual(client_kwargs["max_retries"], 3)          # 자동 재시도
        self.assertIsNotNone(client_kwargs.get("timeout"))         # 명시적 타임아웃

    @patch("openai.OpenAI")
    def test_tool_call_turn_records_calls_and_appends_assistant_message(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_tool_call_completion()
        mock_openai_cls.return_value = mock_client

        session = tool_chat._DeepSeek("deepseek-v4-pro", "시스템", [{"role": "user", "content": "hi"}],
                                      [{"name": "rag_search", "description": "검색", "schema": {"type": "object"}}])
        turn = session.step()

        self.assertEqual(len(turn.calls), 1)
        self.assertEqual(turn.calls[0].name, "rag_search")
        self.assertEqual(turn.calls[0].args, {"query": "실적"})
        self.assertEqual(session.messages[-1]["role"], "assistant")
        self.assertEqual(session.messages[-1]["tool_calls"][0]["function"]["name"], "rag_search")

    def test_add_results_appends_tool_messages(self):
        session = tool_chat._DeepSeek("deepseek-v4-pro", "시스템", [], [])
        call = tool_chat.Call(id="call_1", name="rag_search", args={})
        session.add_results([(call, "검색 결과 본문", True)])
        self.assertEqual(session.messages[-1], {"role": "tool", "tool_call_id": "call_1", "content": "검색 결과 본문"})

    @patch("openai.OpenAI")
    def test_auth_error_maps_to_deepseek_message(self, mock_openai_cls):
        import openai

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = openai.AuthenticationError(
            "bad key", response=MagicMock(status_code=401), body=None)
        mock_openai_cls.return_value = mock_client

        session = tool_chat._DeepSeek("deepseek-v4-pro", "시스템", [{"role": "user", "content": "hi"}], [])
        with self.assertRaises(providers.ChatError) as cm:
            session.step()
        self.assertIn("DeepSeek", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
