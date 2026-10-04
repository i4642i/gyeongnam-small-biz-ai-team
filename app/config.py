import os
from pathlib import Path

from dotenv import load_dotenv

# 시험 서버가 진짜 .env(실제 API 키)를 읽지 않도록 AGENT_TOWN_NO_DOTENV=1 이면 .env 를 읽지 않는다.
if not os.getenv("AGENT_TOWN_NO_DOTENV"):
    load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
# 테스트 등에서 실제 데이터를 건드리지 않도록 AGENT_TOWN_DATA 로 데이터 폴더를 바꿀 수 있다.
DATA_DIR = Path(os.getenv("AGENT_TOWN_DATA") or BASE_DIR / "data")
AGENTS_DIR = DATA_DIR / "agents"
AGENTS_DIR.mkdir(parents=True, exist_ok=True)
OFFICES_DIR = DATA_DIR / "offices"
OFFICES_DIR.mkdir(parents=True, exist_ok=True)
LIBRARIES_DIR = DATA_DIR / "libraries"
LIBRARIES_DIR.mkdir(parents=True, exist_ok=True)

# 공급사 키(Claude / OpenAI / Gemini) 중 하나라도 있으면 live, 하나도 없으면 mock(가짜 답변).
# `ant auth login` 프로필로 Claude를 쓰는 경우 .env 에 LLM_MODE=live 로 직접 알린다. (예전 값 "claude"도 live 로 취급)
_KEY_ENVS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
_has_key = any(os.getenv(k) for k in _KEY_ENVS)
_explicit = (os.getenv("LLM_MODE") or "").lower()
LLM_MODE = "live" if _explicit in ("live", "claude") else "mock" if _explicit == "mock" else ("live" if _has_key else "mock")
if _explicit == "claude":   # 예전 설정 호환: providers.is_configured 가 이 값을 본다
    os.environ["LLM_MODE"] = "live"
