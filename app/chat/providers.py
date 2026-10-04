"""LLM 공급사(Claude / OpenAI / Gemini) 연결. 캐릭터마다 어떤 공급사·모델을 쓸지 model.json 으로 정한다.

공급사별 공식 SDK를 그대로 쓰고, 이 파일이 세 곳의 차이(요청 모양, 오류)를 흡수해서
바깥에는 chat(provider, model, system, messages) 하나만 보여 준다.
API 키는 서버의 환경변수(.env)에서만 읽고, 화면·API 응답으로는 절대 내보내지 않는다.

테스트용으로 서버 주소를 바꿀 수 있다: ANTHROPIC_BASE_URL, OPENAI_BASE_URL, GEMINI_BASE_URL.
"""

import logging
import os
import time

import anthropic

from app.chat import usage

log = logging.getLogger("agent_town.providers")


class ChatError(RuntimeError):
    """사용자에게 그대로 보여줘도 되는 대화 실패 사유."""


class Truncated(Exception):
    """출력이 도중에 끊겼다(예: max_tokens, Claude 전용 — 이 예외를 던지는 쪽은 tool_chat._Claude 뿐이다).
    사람이 "이어서 쓰기/중단"을 고를 수 있게 지금까지 쓴 글과 이어받는 데 필요한 세션 상태를 담아 올린다.
    session_state: {"model", "system", "messages", "tools"} — 전부 JSON 으로 그대로 저장할 수 있는 값만 담는다(재개 시 파일에서 다시 읽는다)."""

    def __init__(self, text: str, trace: list[dict], session_state: dict, resp_usage: dict | None = None):
        super().__init__("output truncated")
        self.text = text
        self.trace = trace
        self.session_state = session_state
        self.resp_usage = resp_usage


class HardTruncated(Exception):
    """출력이 잘렸는데 이어쓰기 세션이 없는 공급사(deepseek/openai). 정상 완료로 저장하지 않는다.
    잘린 초안(text)과 지금까지의 도구 사용 기록(trace), 적용된 출력 한도(applied)를 담아, _execute 가
    '검색 없이 최대 1회 재작성' 복구를 시도하거나 미완료로 종료하는 데 쓴다."""

    def __init__(self, text: str, trace: list[dict], applied: int | None = None):
        super().__init__("output hard-truncated")
        self.text = text
        self.trace = trace
        self.applied = applied


# 내장 모델 목록은 키가 없어서 공급사에 물어볼 수 없을 때 보여 주는 것이다(키가 있으면 공급사의 최신 목록을 쓴다).
# 모델 이름은 자주 바뀌므로 공식 문서에서 확인한 날짜를 함께 화면에 보여 준다. 새 모델은 '직접 입력'으로 쓸 수 있다.
#   Claude : Anthropic 모델 안내 (claude-api 스킬 문서)
#   OpenAI : https://developers.openai.com/api/docs/models , .../pricing
#   Gemini : https://ai.google.dev/gemini-api/docs/models , .../pricing
BUILTIN_MODELS_CHECKED = "2026-09-19"

PROVIDERS = {
    "anthropic": {
        "label": "Claude (Anthropic)",
        "key_envs": ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"],
        "default_model": os.getenv("CHAT_MODEL") or "claude-opus-5",
        "static_models": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "claude-fable-5-1"],
    },
    "openai": {
        "label": "OpenAI",
        "key_envs": ["OPENAI_API_KEY"],
        "default_model": "gpt-5.6-terra",   # 문서: "지능과 비용의 균형"
        "static_models": [
            "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
            "gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano",
            "gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini",
        ],
    },
    "google": {
        "label": "Gemini (Google)",
        "key_envs": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "default_model": "gemini-3.8-flash",
        "static_models": [
            "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
            "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.1-pro-preview",
            "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite",
        ],
    },
    # 딥시크 OpenAI 호환 엔드포인트(2026-09-25). Anthropic 호환(/anthropic)도 있지만, 도구 호출+추론 다회
    # 대화에서 알려진 400 문제(docs/work_orders/설계안_딥시크_활용_2026-09-25.md 참고)가 있어 OpenAI 호환 하나로
    # 통일한다 — 지식그래프 추출(도구 없음)·도구 호출 애널리스트 모두 이 경로 하나만 쓴다.
    "deepseek": {
        "label": "DeepSeek",
        "key_envs": ["DEEPSEEK_API_KEY"],
        "default_model": "deepseek-flash",
        "static_models": ["deepseek-flash", "deepseek-v4-pro"],
    },
}

DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"


def _deepseek_client():
    """딥시크(OpenAI 호환) 클라이언트. 타임아웃과 자동 재시도를 함께 건다(2026-09-25):
    - 연결 15초·읽기 180초: 정상 pro 호출(생각 모드로 길어질 수 있음)은 살리되, 죽은 연결은 openai 기본
      600초(10분)까지 안 매달리고 빨리 실패시킨다. '잠깐 끊김 = 10분 낭비'를 막는다.
    - max_retries=3: 연결 끊김·타임아웃·순간적 rate limit 같은 일시적 오류를 SDK 가 지수 backoff 로 자동
      재시도한다(순간 네트워크 blip 이 업무 전체를 실패시키지 않게)."""
    import httpx
    import openai
    return openai.OpenAI(api_key=_key("deepseek"), base_url=DEEPSEEK_BASE_URL,
                         timeout=httpx.Timeout(180.0, connect=15.0), max_retries=3)

CLAUDE_MAX_TOKENS = 16000      # Claude: 스트리밍 없이도 안전한 상한
# OpenAI/Gemini 의 최신 모델은 '생각하는 데 쓴 토큰'도 출력 한도에 포함된다. 한도가 작으면 생각만 하다가 끝나 빈 답변이 온다.
# 그래서 넉넉히 잡고, 한도가 작은 옛 모델이 거절하면 아래 값으로 낮춰 한 번 더 시도한다.
OPENAI_MAX_TOKENS = 16000
GEMINI_MAX_TOKENS = 16384
FALLBACK_MAX_TOKENS = 4096
LIST_TIMEOUT = 8               # 모델 목록 조회는 오래 기다리지 않는다


def _key(provider: str) -> str | None:
    for env in PROVIDERS[provider]["key_envs"]:
        if os.getenv(env):
            return os.getenv(env)
    return None


def is_configured(provider: str) -> bool:
    if _key(provider):
        return True
    # `ant auth login` 프로필로 Claude를 쓰는 경우: 환경변수 키가 없어서 감지할 수 없으니 LLM_MODE=live 로 직접 알린다
    return provider == "anthropic" and os.getenv("LLM_MODE") == "live"


def key_hint(provider: str) -> str:
    return PROVIDERS[provider]["key_envs"][0]


# ---------------------------------------------------------------- 대화

_anthropic_client: anthropic.Anthropic | None = None


def claude_client() -> anthropic.Anthropic:
    """공용 Claude 클라이언트(도구 호출 반복도 같은 것을 쓴다)."""
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic()
    return _anthropic_client


def _claude(model: str, system: str, messages: list[dict]) -> str:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic()
    kwargs = dict(model=model, max_tokens=CLAUDE_MAX_TOKENS, system=system, messages=messages)
    if "haiku" not in model:
        kwargs["output_config"] = {"effort": "medium"}   # Haiku 는 effort 를 받지 않는다
    if model in ("claude-opus-5", "claude-fable-5-1"):
        # 안전 분류기가 요청을 거절하면 서버가 대체 모델로 자동 재시도한다.
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    try:
        resp = _anthropic_client.beta.messages.create(**kwargs)
        usage.from_claude(resp)
    except anthropic.AuthenticationError:
        raise ChatError("Claude 인증에 실패했습니다. .env의 ANTHROPIC_API_KEY를 확인하세요.")
    except anthropic.NotFoundError:
        raise ChatError(f"Claude 모델을 찾을 수 없습니다: {model}")
    except anthropic.RateLimitError:
        raise ChatError("Claude 요청이 너무 많습니다. 잠시 후 다시 시도하세요.")
    except anthropic.APIConnectionError:
        raise ChatError("Claude 서버에 연결하지 못했습니다. 인터넷 연결을 확인하세요.")
    except anthropic.APIStatusError as e:
        raise ChatError(f"Claude 호출 실패 ({e.status_code}): {e.message}")

    if resp.stop_reason == "refusal":
        raise ChatError("이 요청에는 답할 수 없습니다.")
    text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise ChatError("빈 답변이 왔습니다. 다시 시도하세요.")
    return text


_HIT_LIMIT = "모델이 답을 쓰기 전에 출력 한도에 도달했습니다(생각하는 데 토큰을 다 썼을 수 있습니다). 다시 시도하거나 다른 모델을 고르세요."


def _mentions_token_limit(message: str) -> bool:
    m = (message or "").lower()
    return any(k in m for k in ("max_completion_tokens", "max_tokens", "maxoutputtokens", "max_output_tokens"))


def _output_cap_exceeded(message: str) -> bool:
    """요청한 '출력' 한도가 모델의 출력 상한을 넘어 거부된 것이 명확한 경우만 True.
    입력/컨텍스트 초과(메시지가 너무 김)는 출력 한도를 낮춰도 해결되지 않으므로 fallback 대상이 아니다 — 배제한다."""
    m = (message or "").lower()
    if not any(k in m for k in ("max_completion_tokens", "max_tokens", "maxoutputtokens", "max_output_tokens")):
        return False
    if any(k in m for k in ("context length", "context_length", "context window", "context_length_exceeded",
                            "reduce the length of the messages", "input tokens", "prompt tokens", "too many tokens in")):
        return False   # 입력/컨텍스트 초과 신호가 있으면 출력 상한 문제가 아니다
    return True


def _openai(model: str, system: str, messages: list[dict]) -> str:
    import openai

    client = openai.OpenAI(api_key=_key("openai"))

    def call(limit: int):
        r = client.chat.completions.create(
            model=model, max_completion_tokens=limit, messages=[{"role": "system", "content": system}, *messages],
        )
        usage.from_openai(r)
        return r

    try:
        try:
            resp = call(OPENAI_MAX_TOKENS)
        except openai.BadRequestError as e:
            if not _mentions_token_limit(e.message):
                raise
            resp = call(FALLBACK_MAX_TOKENS)   # 출력 한도가 작은 모델이면 낮춰서 한 번 더
    except openai.AuthenticationError:
        raise ChatError("OpenAI 인증에 실패했습니다. .env의 OPENAI_API_KEY를 확인하세요.")
    except openai.NotFoundError:
        raise ChatError(f"OpenAI 모델을 찾을 수 없습니다: {model}")
    except openai.RateLimitError:
        raise ChatError("OpenAI 요청이 너무 많거나 사용 한도를 넘었습니다.")
    except openai.APIConnectionError:
        raise ChatError("OpenAI 서버에 연결하지 못했습니다. 인터넷 연결을 확인하세요.")
    except openai.APIStatusError as e:
        raise ChatError(f"OpenAI 호출 실패 ({e.status_code}): {e.message}")

    choice = resp.choices[0]
    if choice.finish_reason == "content_filter":
        raise ChatError("OpenAI가 이 요청의 답변을 차단했습니다.")
    text = (choice.message.content or "").strip()
    if not text:
        raise ChatError(_HIT_LIMIT if choice.finish_reason == "length" else "빈 답변이 왔습니다. 다시 시도하세요.")
    return text


def _deepseek(model: str, system: str, messages: list[dict]) -> str:
    """딥시크 OpenAI 호환 엔드포인트(_openai() 와 같은 모양, base_url 만 다름). 도구 호출 없는 단발 호출
    (지식그래프 추출 등)에 먼저 쓴다 — 도구 호출 루프(app/chat/tool_chat.py)는 별도로 연결한다."""
    import openai

    client = _deepseek_client()

    def call(limit: int):
        r = client.chat.completions.create(
            model=model, max_completion_tokens=limit, messages=[{"role": "system", "content": system}, *messages],
        )
        usage.from_openai(r)
        return r

    try:
        try:
            resp = call(OPENAI_MAX_TOKENS)
        except openai.BadRequestError as e:
            if not _mentions_token_limit(e.message):
                raise
            resp = call(FALLBACK_MAX_TOKENS)
    except openai.AuthenticationError:
        raise ChatError("DeepSeek 인증에 실패했습니다. .env의 DEEPSEEK_API_KEY를 확인하세요.")
    except openai.NotFoundError:
        raise ChatError(f"DeepSeek 모델을 찾을 수 없습니다: {model}")
    except openai.RateLimitError:
        raise ChatError("DeepSeek 요청이 너무 많거나 사용 한도를 넘었습니다.")
    except openai.APIConnectionError:
        raise ChatError("DeepSeek 서버에 연결하지 못했습니다. 인터넷 연결을 확인하세요.")
    except openai.APIStatusError as e:
        raise ChatError(f"DeepSeek 호출 실패 ({e.status_code}): {e.message}")

    choice = resp.choices[0]
    if choice.finish_reason == "content_filter":
        raise ChatError("DeepSeek가 이 요청의 답변을 차단했습니다.")
    text = (choice.message.content or "").strip()
    if not text:
        raise ChatError(_HIT_LIMIT if choice.finish_reason == "length" else "빈 답변이 왔습니다. 다시 시도하세요.")
    return text


def _gemini_client(timeout_ms: int | None = None):
    from google import genai
    from google.genai import types

    http = {}
    if os.getenv("GEMINI_BASE_URL"):
        http["base_url"] = os.getenv("GEMINI_BASE_URL")
    if timeout_ms:
        http["timeout"] = timeout_ms
    return genai.Client(api_key=_key("google"), http_options=types.HttpOptions(**http) if http else None)


def _gemini(model: str, system: str, messages: list[dict]) -> str:
    from google.genai import errors, types

    contents = [
        types.Content(role="user" if m["role"] == "user" else "model", parts=[types.Part(text=m["content"])])
        for m in messages
    ]
    # 클라이언트를 변수로 붙잡아 둔다: 임시 객체로 쓰면 곧바로 정리되면서 내부 연결이 닫혀 요청이 실패한다
    client = _gemini_client()

    def call(limit: int):
        r = client.models.generate_content(
            model=model, contents=contents,
            config=types.GenerateContentConfig(system_instruction=system, max_output_tokens=limit),
        )
        usage.from_gemini(r)
        return r

    try:
        try:
            resp = call(GEMINI_MAX_TOKENS)
        except errors.APIError as e:
            if e.code != 400 or not _mentions_token_limit(e.message):
                raise
            resp = call(FALLBACK_MAX_TOKENS * 2)   # 출력 한도가 작은 모델이면 낮춰서 한 번 더
    except errors.APIError as e:
        if e.code in (401, 403):
            raise ChatError("Gemini 인증에 실패했습니다. .env의 GEMINI_API_KEY를 확인하세요.")
        if e.code == 404:
            raise ChatError(f"Gemini 모델을 찾을 수 없습니다: {model}")
        if e.code == 429:
            raise ChatError("Gemini 요청이 너무 많거나 사용 한도를 넘었습니다.")
        raise ChatError(f"Gemini 호출 실패 ({e.code}): {e.message}")
    except Exception as e:   # 연결 실패 등 SDK 밖의 오류
        log.exception("Gemini 호출 중 예기치 않은 오류")
        raise ChatError(f"Gemini 서버에 연결하지 못했습니다: {type(e).__name__}")

    text = (resp.text or "").strip() if resp.candidates else ""
    if not text:
        block = getattr(resp.prompt_feedback, "block_reason", None) if resp.prompt_feedback else None
        if block:
            raise ChatError("Gemini가 이 요청의 답변을 차단했습니다.")
        reason = str(getattr(resp.candidates[0], "finish_reason", "")) if resp.candidates else ""
        raise ChatError(_HIT_LIMIT if "MAX_TOKENS" in reason else "빈 답변이 왔습니다. 다시 시도하세요.")
    return text


_CHAT = {"anthropic": _claude, "openai": _openai, "google": _gemini, "deepseek": _deepseek}


def chat(provider: str, model: str, system: str, messages: list[dict]) -> str:
    """messages: [{role: user|assistant, content: str}, ...] — 마지막이 새 사용자 메시지."""
    if provider not in PROVIDERS:
        raise ChatError(f"알 수 없는 공급사입니다: {provider}")
    if not model:
        raise ChatError("이 캐릭터에 모델이 정해져 있지 않습니다. 캐릭터의 '모델' 설정을 확인하세요.")
    if not is_configured(provider):
        raise ChatError(f"{PROVIDERS[provider]['label']} 키가 설정되어 있지 않습니다. .env에 {key_hint(provider)} 를 넣고 서버를 다시 시작하세요.")
    return _CHAT[provider](model, system, messages)


# ---------------------------------------------------------------- 모델 목록

_NON_CHAT = ("embedding", "whisper", "tts", "dall-e", "moderation", "image", "audio", "transcribe", "realtime", "davinci", "babbage", "search", "live")
_cache: dict[str, tuple[float, list[str]]] = {}
CACHE_SECONDS = 600


def _list_live(provider: str) -> list[str]:
    if provider == "anthropic":
        client = anthropic.Anthropic(timeout=LIST_TIMEOUT)
        return [m.id for m in client.models.list()]
    if provider == "openai":
        import openai

        ids = [m.id for m in openai.OpenAI(api_key=_key("openai"), timeout=LIST_TIMEOUT).models.list()]
        return [i for i in ids if not any(w in i.lower() for w in _NON_CHAT)]
    if provider == "google":
        out = []
        client = _gemini_client(timeout_ms=LIST_TIMEOUT * 1000)   # 변수로 붙잡아 둔다 (위와 같은 이유)
        for m in client.models.list():
            if "generateContent" in (getattr(m, "supported_actions", None) or []):
                out.append((m.name or "").removeprefix("models/"))
        return out
    return []


def list_models(provider: str) -> tuple[list[str], str]:
    """(모델 이름 목록, 출처) — 출처는 "live"(공급사에 직접 물어 받은 최신 목록) 또는 "builtin"(내장 목록).
    키가 있으면 공급사에 물어(10분 캐시), 키가 없거나 실패하면 내장 목록을 돌려준다. 실패해도 예외를 내지 않는다."""
    static = list(PROVIDERS[provider]["static_models"])
    if not is_configured(provider):
        return static, "builtin"
    hit = _cache.get(provider)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1], "live"
    try:
        models = sorted(set(_list_live(provider)))
    except Exception:
        log.warning("%s 모델 목록을 불러오지 못했습니다", provider, exc_info=True)
        return static, "builtin"
    if not models:
        return static, "builtin"
    _cache[provider] = (time.time(), models)
    return models, "live"
