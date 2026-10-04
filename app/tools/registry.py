"""도구 등록소: 캐릭터가 대화 중에 스스로 호출할 수 있는 도구의 정의.

- 도구 목록은 서버 전체에 하나(`data/tools.json`)이고, 캐릭터마다 그중 무엇을 쓸지 고른다(캐릭터 폴더의 `tools.json`).
- 1단계는 **API 도구**(주소·파라미터를 정의해서 HTTP로 부르는 도구)만 있고, **읽기 전용**(상태를 바꾸지 않는 조회·검색)이어야 한다.
  함수 도구·MCP 서버는 코드를 실행하므로 실행 전 확인이 필요해서 이후 단계에서 만든다.
- 키는 `.env`에만 두고, 정의에는 `${NAME}`처럼 이름만 쓴다.
  화면·API 응답으로 키 값을 내보내지 않는다(키가 설정됐는지만 알려 준다).
"""

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import DATA_DIR

log = logging.getLogger("agent_town.tools")
TOOLS_FILE = DATA_DIR / "tools.json"
_lock = threading.Lock()

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
WIRE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\[\]]{0,59}$")   # 서비스가 실제로 받는 이름(예: conditions[publication_date][gte])
# ${NAME} 또는 ${NAME:-기본값}: 환경변수(.env) 참조. 이름은 대문자만.
ENV_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]{1,63})(?::-([^}]*))?\}")

# 한 번에 쓰는 한도(모두 환경변수로 바꿀 수 있다)
MAX_STEPS = int(os.getenv("TOOL_MAX_STEPS") or 5)                      # 질문 하나에서 모델이 도구를 부르는 반복 횟수
MAX_CALLS_PER_TOOL = int(os.getenv("TOOL_MAX_CALLS_PER_TOOL") or 3)    # 질문 하나에서 같은 도구를 부르는 횟수
# 업무 실행(리포트처럼 자료를 많이 모으는 일)에서는 한도가 더 넉넉하다. 도구마다 max_calls / max_calls_task 로 따로 정할 수도 있다.
MAX_STEPS_TASK = int(os.getenv("TOOL_MAX_STEPS_TASK") or 20)   # 2단계 앵커 검색(넓은 발견+후보별 anchor_only+page_fetch)이 라운드를 많이 써서 15→20 (2026-09-26)
# 업무 실행에서 한 라운드에 실행하는 '검색·조회' 도구 수(2026-09-26). 한 라운드에 10개씩 불러 결과(W태그 284개)가 한꺼번에 쌓인 실패 후.
# 계산 도구는 결과가 짧고 신호마다 한꺼번에 부르므로 이 상한에 넣지 않는다(tool_chat 의 전체 병렬 상한만 적용).
MAX_SEARCH_PER_TURN_TASK = int(os.getenv("TOOL_MAX_SEARCH_PER_TURN_TASK") or 4)
MAX_CALLS_PER_TOOL_TASK = int(os.getenv("TOOL_MAX_CALLS_PER_TOOL_TASK") or 10)
DEFAULT_DAILY_LIMIT = int(os.getenv("TOOL_DAILY_LIMIT") or 20)         # 캐릭터 하나가 같은 도구를 하루에 부르는 횟수
DEFAULT_SEARCH_BUDGET = int(os.getenv("RUN_SEARCH_BUDGET") or 60)      # 실행 전체 검색 예산(단계별 조사, 업무별 search_budget 로 조정)
# 실행 전체 '대화 전체 글자' 소프트 권장선(문자 수, 토큰 아님). 2026-09-26: 도구 결과만 세던 200,000 을 대화 전체(시스템·도구 정의·입력·
# 모델 출력·도구 결과)로 바꾸면서 250,000 으로 옮김 — 지정학 기준 기본분 약 5만을 더한 같은 그릇이다(키운 것이 아님). 약 8.6만 토큰.
DEFAULT_CONTEXT_SOFT_CHARS = int(os.getenv("RUN_CONTEXT_SOFT_CHARS") or 250000)

# '검색 도구'(외부 정보를 가져오는 도구) — 실행 전체 검색 예산(app/runs/budget.py)에 집계된다.
# calc_*·check_*·sector_series_stats·sector_deflate·horizon_weight 같은 계산·판정 도구는 검색이 아니므로 제외한다.
SEARCH_TOOL_IDS = {"page_fetch"}


def is_search_tool(tool_or_id) -> bool:
    tid = getattr(tool_or_id, "id", tool_or_id)
    return tid in SEARCH_TOOL_IDS


# '조회 도구'(2026-09-26): 자료를 더 가져오는 도구 = 검색 도구 + 자료·종목·기업 정보 조회. 문맥 전환선(75%)에서 꺼지고, 라운드당 상한이 걸린다.
# 계산·판정·조립 도구(calc_*, check_*, head_score_universe, pm_build_portfolio, peer_rank, verify_overall …)는 보고서를 완성하는 데
# 필요하고 결과가 짧아(계산 도구 평균 200~500자) 끄지 않는다. 마지막 '작성 전용' 라운드에서는 모든 도구가 꺼진다.
RETRIEVAL_TOOL_IDS = SEARCH_TOOL_IDS | {"rag_search", "office_computer", "graph_search", "company_facts", "head_lookup_security", "head_industry_members"}
# 결과가 작은 조회 도구(결과 상한 3,000자 이하, 예: fred_release_dates 평균 533자)는 라운드당 상한에서 뺀다 — 산업 기자가 핵심 시리즈
# 14개의 발표일을 한 라운드에 확인하는 절차가 상한 때문에 4라운드로 늘어나지 않게.
SMALL_RESULT_CHARS = int(os.getenv("TOOL_SMALL_RESULT_CHARS") or 3000)


def is_retrieval_tool(tool_or_id) -> bool:
    tid = getattr(tool_or_id, "id", tool_or_id)
    return tid in RETRIEVAL_TOOL_IDS


def is_round_capped(tool) -> bool:
    """라운드당 조회 상한(MAX_SEARCH_PER_TURN_TASK)에 드는 도구인가 — 결과가 큰 조회 도구만."""
    return is_retrieval_tool(tool) and (getattr(tool, "max_chars", None) or DEFAULT_MAX_CHARS) > SMALL_RESULT_CHARS
DEFAULT_TIMEOUT = 20                                                    # 도구 한 번의 시간 제한(초)
DEFAULT_MAX_CHARS = 6000                                                # 도구 결과를 모델에 돌려주는 길이 상한


class Param(BaseModel):
    name: str
    type: str = "string"
    description: str = Field(default="", max_length=300)
    required: bool = False
    enum: list[str] | None = Field(default=None, max_length=20)
    minimum: float | None = None
    maximum: float | None = None
    wire_name: str | None = Field(default=None, max_length=60)          # 서비스에 보낼 때 쓰는 이름(비우면 name). 대괄호가 든 이름을 모델이 쓰기 편한 이름으로 바꿔 준다
    value_map: dict[str, str] | None = Field(default=None, max_length=30)   # 모델이 준 값 → 서비스에 보낼 값(예: rule → RULE)

    @field_validator("wire_name")
    @classmethod
    def _wire(cls, v):
        if v is not None and not WIRE_RE.match(v):
            raise ValueError("전송 이름은 영문으로 시작하고 영문·숫자·밑줄·점·대괄호만 쓸 수 있습니다(60자 이내).")
        return v or None

    @field_validator("value_map")
    @classmethod
    def _vmap(cls, v):
        if v is not None:
            if any(len(str(k)) > 60 or len(str(x)) > 60 for k, x in v.items()):
                raise ValueError("값 바꾸기의 각 값은 60자 이내여야 합니다.")
        return v or None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if not PARAM_RE.match(v):
            raise ValueError("파라미터 이름은 영문 소문자로 시작하고 영문 소문자·숫자·밑줄만 쓸 수 있습니다(40자 이내).")
        return v

    @field_validator("type")
    @classmethod
    def _type(cls, v):
        if v not in ("string", "number", "integer", "boolean"):
            raise ValueError("파라미터 종류는 string / number / integer / boolean 중 하나여야 합니다.")
        return v


class Http(BaseModel):
    method: str = "GET"
    url: str = Field(max_length=500)
    headers: dict[str, str] = Field(default_factory=dict)
    send: str = "query"                                     # 모델이 준 값을 query 로 보낼지 JSON 본문(json)으로 보낼지
    extra: dict[str, str | int | float | bool | list[str]] = Field(default_factory=dict)   # 항상 함께 보내는 고정 값(목록이면 같은 이름으로 여러 번 보낸다)

    @field_validator("method")
    @classmethod
    def _method(cls, v):
        v = v.upper()
        if v not in ("GET", "POST"):
            raise ValueError("1단계에서는 GET 과 POST 만 쓸 수 있습니다(읽기 전용 조회).")
        return v

    @field_validator("send")
    @classmethod
    def _send(cls, v):
        if v not in ("query", "json"):
            raise ValueError("send 는 query 또는 json 이어야 합니다.")
        return v

    @field_validator("url")
    @classmethod
    def _url(cls, v):
        v = v.strip()
        # 환경변수 자리(${NAME}, ${NAME:-기본값})는 기본값(없으면 임시 주소)으로 바꿔서 검사한다. 실행할 때 실제 값으로 다시 검사한다.
        probe = ENV_RE.sub(lambda m: m.group(2) if m.group(2) else "https://placeholder.invalid", v)
        if not re.match(r"^https?://", probe):
            raise ValueError("주소는 http:// 또는 https:// 로 시작해야 합니다.")
        if re.match(r"^https?://[^/]*@", probe):
            raise ValueError("주소에 사용자 이름·비밀번호를 넣지 마세요. 키는 헤더에 ${환경변수}로 참조하세요.")
        return v

    @field_validator("headers")
    @classmethod
    def _headers(cls, v):
        if len(v) > 10:
            raise ValueError("헤더는 10개까지 넣을 수 있습니다.")
        for k, val in v.items():
            if not re.match(r"^[A-Za-z0-9-]{1,50}$", k) or len(val) > 400:
                raise ValueError(f"헤더 '{k}'가 올바르지 않습니다.")
        return v

    @field_validator("extra")
    @classmethod
    def _extra(cls, v):
        if len(v) > 10:
            raise ValueError("고정 값은 10개까지 넣을 수 있습니다.")
        for k, val in v.items():
            if isinstance(val, list) and (len(val) > 20 or any(len(x) > 100 for x in val)):
                raise ValueError(f"고정 값 '{k}' 목록은 20개까지, 항목은 100자까지입니다.")
        return v


FORMATS = ("json", "page_fetch", "rag_search", "office_computer",
           "graph_search", "rag_index", "news_collect", "naver_news")


class ToolDef(BaseModel):
    id: str
    label: str = Field(default="", max_length=40)
    description: str = Field(min_length=1, max_length=600)
    read_only: bool = False
    params: list[Param] = Field(default_factory=list, max_length=20)
    http: Http
    timeout: int = Field(default=DEFAULT_TIMEOUT, ge=1, le=60)
    max_chars: int = Field(default=DEFAULT_MAX_CHARS, ge=500, le=20000)
    daily_limit: int = Field(default=DEFAULT_DAILY_LIMIT, ge=1, le=1000)
    max_calls: int | None = Field(default=None, ge=1, le=100)        # 대화 중 질문 하나에서 이 도구를 부를 수 있는 횟수(비우면 기본값)
    max_calls_task: int | None = Field(default=None, ge=1, le=1000)  # 업무 실행 중 한 번의 실행에서(비우면 기본값)
    format: str = "json"                                   # 결과를 어떻게 정리할지(FORMATS 중 하나)
    # 누가 쓰는 도구인가: agent = 직원이 대화·업무 중에 부른다 / computer = 회사 컴퓨터가 실행해 결과를 문서로 보관한다
    # (결과가 문서 전체라 직원 대화에 넣기엔 큰 도구). 컴퓨터는 agent 도구도 실행해 결과를 보관할 수 있다.
    usage: str = "agent"

    @field_validator("id")
    @classmethod
    def _id(cls, v):
        if not NAME_RE.match(v):
            raise ValueError("도구 이름은 영문 소문자로 시작하고 영문 소문자·숫자·밑줄만 쓸 수 있습니다(2~40자). 모델이 부르는 이름입니다.")
        return v

    @model_validator(mode="after")
    def _check(self):
        if not self.read_only:
            raise ValueError("1단계에서는 읽기 전용 도구(검색·조회처럼 상태를 바꾸지 않는 것)만 등록할 수 있습니다. 파일·시스템을 바꾸는 도구는 실행 전 확인 기능과 함께 이후 단계에서 만듭니다.")
        names = [p.name for p in self.params]
        if len(set(names)) != len(names):
            raise ValueError("파라미터 이름이 겹칩니다.")
        wires = [p.wire_name or p.name for p in self.params]
        if len(set(wires)) != len(wires):
            raise ValueError("서비스로 보내는 이름(전송 이름)이 겹칩니다.")
        if (set(names) | set(wires)) & set(self.http.extra):
            raise ValueError("고정 값의 이름이 파라미터 이름과 겹칩니다.")
        for p in self.params:
            if p.value_map and p.enum and not set(p.enum) >= set(p.value_map):
                raise ValueError(f"'{p.name}'의 값 바꾸기에 고르기 값에 없는 항목이 있습니다.")
        if self.format not in FORMATS:
            raise ValueError(f"결과 형식은 {', '.join(FORMATS)} 중 하나입니다.")
        if self.usage not in ("agent", "computer"):
            raise ValueError("쓰는 곳(usage)은 agent(직원) 또는 computer(회사 컴퓨터)여야 합니다.")
        if self.format == "rag_index" and self.usage != "computer":
            raise ValueError("rag_index 형식은 회사 컴퓨터 도구(usage=computer)로만 등록할 수 있습니다.")
        if self.format == "page_fetch" and not any(p.name == "url" and p.type == "string" and p.required for p in self.params):
            raise ValueError("page_fetch 형식의 도구에는 필수 글 인자 'url' 이 있어야 합니다.")
        if self.format in ("rag_search", "office_computer", "graph_search") and not any(p.name == "query" and p.type == "string" and p.required for p in self.params):
            raise ValueError(f"{self.format} 형식의 도구에는 필수 글 인자 'query' 가 있어야 합니다.")
        return self


def calls_limit(tool: "ToolDef", mode: str = "chat") -> int:
    """질문(또는 업무 실행) 하나에서 이 도구를 부를 수 있는 횟수."""
    if mode == "task":
        return tool.max_calls_task or max(MAX_CALLS_PER_TOOL_TASK, tool.max_calls or 0)
    return tool.max_calls or MAX_CALLS_PER_TOOL


def steps_limit(mode: str = "chat") -> int:
    return MAX_STEPS_TASK if mode == "task" else MAX_STEPS


# ---------------------------------------------------------------- 내장 도구

# 회사 컴퓨터 관련 기본 도구(2026-09-27) — 플랫폼 기능이라 어느 설치본에나 있어야 한다(데이터 폴더의 tools.json 이 아니라 코드에 둔다).
OFFICE_COMPUTER = ToolDef(**{
    "id": "office_computer",
    "label": "회사 컴퓨터",
    "description": "앉은 회사에 놓인 컴퓨터(예: 공시 컴퓨터)에서 문서를 찾아 관련 조각을 돌려준다. ticker·doc_type 을 주면 그 문서만 본다(다른 회사·다른 종류 문서 내용이 안 섞인다) — 한 회사만 다루는 업무는 찾을 때마다 ticker 를 넣을 것. 컴퓨터가 여러 대면 computer 에 이름을 넣어 한 대만 볼 수 있다.",
    "read_only": True,
    "params": [
        {
            "name": "query",
            "type": "string",
            "description": "찾을 내용(구체적으로 — 낱말 하나보다 짧은 문장이 더 잘 맞는다)",
            "required": True,
            "enum": None,
            "minimum": None,
            "maximum": None,
            "wire_name": None,
            "value_map": None
        },
        {
            "name": "ticker",
            "type": "string",
            "description": "이 티커의 문서만 본다(문서 맨 앞 정보표의 '티커'와 일치해야 함). 비우면 전체에서 찾는다",
            "required": False,
            "enum": None,
            "minimum": None,
            "maximum": None,
            "wire_name": None,
            "value_map": None
        },
        {
            "name": "doc_type",
            "type": "string",
            "description": "이 문서 종류만 본다(예: 10-K, 10-Q, Earnings Call Transcript — 문서 맨 앞 정보표의 '문서 종류'와 정확히 일치해야 함). 비우면 모든 종류에서 찾는다",
            "required": False,
            "enum": None,
            "minimum": None,
            "maximum": None,
            "wire_name": None,
            "value_map": None
        },
        {
            "name": "k",
            "type": "number",
            "description": "가져올 조각 수(1~10, 기본 6)",
            "required": False,
            "enum": None,
            "minimum": 1,
            "maximum": 10,
            "wire_name": None,
            "value_map": None
        },
        {
            "name": "computer",
            "type": "string",
            "description": "이 이름의 컴퓨터만 본다(예: 공시 컴퓨터). 비우면 회사에 놓인 모든 컴퓨터에서 찾는다",
            "required": False,
            "enum": None,
            "minimum": None,
            "maximum": None,
            "wire_name": None,
            "value_map": None
        }
    ],
    "http": {
        "method": "GET",
        "url": "https://office-computer.invalid/",
        "headers": {},
        "send": "query",
        "extra": {}
    },
    "timeout": 15,
    "max_chars": 8000,
    "daily_limit": 1000,
    "max_calls": None,
    "max_calls_task": 16,
    "format": "office_computer"
})
RAG_INDEX = ToolDef(
    id="rag_index", label="문서 색인", usage="computer", format="rag_index", read_only=True, daily_limit=1000,
    description=("컴퓨터에 들어온 문서를 700자 조각으로 나누고 조각마다 의미 벡터를 만들어, 직원이 '회사 컴퓨터'로 찾을 수 있게 한다. "
                 "이 도구가 설치된 컴퓨터만 새로 받은 문서를 자동으로 색인한다(없으면 보관만). 색인 계산은 이 PC 의 색인 일꾼이 한 줄로 처리한다."),
    params=[{"name": "target", "type": "string", "description": "새 문서만(보관만·실패한 문서) 또는 전체 다시(색인을 지우고 모든 문서)",
             "enum": ["새 문서만", "전체 다시"]}],
    http={"method": "GET", "url": "https://rag-index.invalid/"},
)

NEWS_COLLECT = ToolDef(
    id="news_collect", label="뉴스 모으기", usage="computer", format="news_collect", read_only=True, daily_limit=100, timeout=60,
    description=("관측 기간 동안 미국 상장 기업 관련 기사를 모아 기사 한 건을 문서 한 건으로 보관한다(Tavily 뉴스 검색, 검색어 묶음은 "
                 "data/config/news_collect.json). 재게재 기사는 하나로 합치고 몇 곳에 실렸는지 기록한다. 이미 가진 기사는 다시 받지 않는다."),
    params=[{"name": "window_start", "type": "string", "description": "관측 시작일 YYYY-MM-DD(비우면 끝 6일 전)"},
            {"name": "window_end", "type": "string", "description": "관측 종료일 YYYY-MM-DD(비우면 오늘)"},
            {"name": "queries", "type": "string", "description": "검색어(줄바꿈·세미콜론 구분). 비우면 설정 파일의 묶음"}],
    http={"method": "GET", "url": "https://news-collect.invalid/"},
)

NAVER_NEWS = ToolDef(
    id="naver_news", label="국내 뉴스 모으기", usage="computer", format="naver_news", read_only=True, daily_limit=100, timeout=60,
    description=("관측 기간 동안 국내 상장 기업(KOSPI) 관련 기사를 모아 기사 한 건을 문서 한 건으로 보관한다(네이버 뉴스 검색 API, "
                 "검색어 묶음은 data/config/naver_news.json). 이 API는 기간 지정이 안 돼 최신순으로 받다가 관측 시작일보다 오래된 "
                 "기사가 나오면 멈춘다. 재게재 기사는 하나로 합치고 몇 곳에 실렸는지 기록한다. 이미 가진 기사는 다시 받지 않는다."),
    params=[{"name": "window_start", "type": "string", "description": "관측 시작일 YYYY-MM-DD(비우면 끝 6일 전)"},
            {"name": "window_end", "type": "string", "description": "관측 종료일 YYYY-MM-DD(비우면 오늘)"},
            {"name": "queries", "type": "string", "description": "검색어(줄바꿈·세미콜론 구분). 비우면 설정 파일의 묶음"}],
    http={"method": "GET", "url": "https://naver-news.invalid/"},
)

# 대회 출품작에서 쓰지 않는 기본 도구(웹 검색·SEC 공시·Tavily 뉴스 수집)는 목록에서 뺐다(2026-10-01). 정의는 위에 남아 있어 필요하면 아래에 다시 넣으면 된다.
BUILTIN = {"office_computer": OFFICE_COMPUTER, "rag_index": RAG_INDEX, "naver_news": NAVER_NEWS}


# ---------------------------------------------------------------- 저장

def _read_custom() -> list[dict]:
    try:
        return json.loads(TOOLS_FILE.read_text(encoding="utf-8")).get("tools", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _write_custom(items: list[dict]) -> None:
    TOOLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOOLS_FILE.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps({"tools": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    for i in range(10):   # 윈도우에서 다른 프로그램이 파일을 잡고 있으면 잠깐 기다린다
        try:
            os.replace(tmp, TOOLS_FILE)
            return
        except PermissionError:
            time.sleep(0.05 * (i + 1))
    os.replace(tmp, TOOLS_FILE)


_BROKEN: dict[str, str] = {}   # 도구 id → 목록에서 빠진 이유(손으로 고친 tools.json 이 규칙에 어긋날 때)


def broken() -> dict[str, str]:
    """지금 목록에서 빠진 도구와 이유 — 화면(/api/tools)·로그에 알린다(2026-09-28, 숙제 B8)."""
    all_tools()
    return dict(_BROKEN)


def all_tools() -> list[ToolDef]:
    out = list(BUILTIN.values())
    seen: dict[str, str] = {}
    for raw in _read_custom():
        try:
            out.append(ToolDef(**raw))
        except Exception as e:   # 손으로 고치다 깨진 항목은 건너뛰되, 조용히 사라지지 않게 알린다
            first = e.errors()[0] if hasattr(e, "errors") and e.errors() else {}
            where = ".".join(str(x) for x in first.get("loc", []))
            reason = f"{where}: {str(first.get('msg', e))[:160]}" if where else str(e)[:200]
            tid = str(raw.get("id") if isinstance(raw, dict) else "?")
            seen[tid] = reason
            if _BROKEN.get(tid) != reason:
                log.warning("도구 '%s' 가 규칙에 어긋나 목록에서 빠졌습니다 — %s (data/tools.json 을 고치세요)", tid, reason)
    _BROKEN.clear()
    _BROKEN.update(seen)
    return out


def get(tool_id: str) -> ToolDef | None:
    return next((t for t in all_tools() if t.id == tool_id), None)


class ToolError(ValueError):
    def __init__(self, message: str, code: int = 422):
        super().__init__(message)
        self.code = code


def _validated(defn: ToolDef) -> ToolDef:
    """저장하기 전에 항상 규칙을 다시 검사한다(model_copy 등으로 만든 정의는 검사를 거치지 않으므로).
    검사에 실패한 항목은 목록을 읽을 때 조용히 버려지므로, 저장 단계에서 막아야 도구가 사라지지 않는다."""
    try:
        return ToolDef.model_validate(defn.model_dump())
    except Exception as e:
        first = e.errors()[0] if hasattr(e, "errors") and e.errors() else {}
        where = ".".join(str(x) for x in first.get("loc", []))
        raise ToolError(f"도구 정의가 올바르지 않아 저장하지 않았습니다{': ' + where if where else ''} — {str(first.get('msg', e))[:160]}", 422)


def create(defn: ToolDef) -> ToolDef:
    defn = _validated(defn)
    with _lock:
        if defn.id in BUILTIN or any(t.id == defn.id for t in all_tools()):
            raise ToolError(f"'{defn.id}' 이름의 도구가 이미 있습니다.", 409)
        items = _read_custom()
        if len(items) >= 50:
            raise ToolError("도구는 50개까지 만들 수 있습니다.", 409)
        items.append(defn.model_dump())
        _write_custom(items)
    return defn


def update(tool_id: str, defn: ToolDef) -> ToolDef:
    if tool_id in BUILTIN:
        raise ToolError("내장 도구는 고칠 수 없습니다. 같은 기능이 필요하면 새 도구를 만들어 쓰세요.", 409)
    if defn.id != tool_id:
        raise ToolError("도구 이름(id)은 바꿀 수 없습니다. 새로 만들고 예전 것을 지우세요.", 422)
    defn = _validated(defn)
    with _lock:
        items = _read_custom()
        for i, raw in enumerate(items):
            if raw.get("id") == tool_id:
                items[i] = defn.model_dump()
                _write_custom(items)
                return defn
    raise ToolError("도구를 찾을 수 없습니다.", 404)


def delete(tool_id: str) -> None:
    if tool_id in BUILTIN:
        raise ToolError("내장 도구는 지울 수 없습니다.", 409)
    with _lock:
        items = _read_custom()
        kept = [r for r in items if r.get("id") != tool_id]
        if len(kept) == len(items):
            raise ToolError("도구를 찾을 수 없습니다.", 404)
        _write_custom(kept)


# ---------------------------------------------------------------- 환경변수 참조

def env_names(tool: ToolDef) -> list[str]:
    """이 도구가 참조하는 환경변수 중 기본값이 없는 것(반드시 있어야 하는 것)."""
    found = []
    texts = [tool.http.url, *tool.http.headers.values(), *[str(v) for v in tool.http.extra.values()]]
    for t in texts:
        for m in ENV_RE.finditer(t):
            if m.group(2) is None and m.group(1) not in found:
                found.append(m.group(1))
    return found


def env_missing(tool: ToolDef) -> list[str]:
    return [n for n in env_names(tool) if not os.getenv(n)]


def substitute(text: str) -> str:
    """${NAME} / ${NAME:-기본값} 을 환경변수 값으로 바꾼다. 값이 없으면 빈 문자열(호출 전에 env_missing 으로 걸러진다)."""
    return ENV_RE.sub(lambda m: os.getenv(m.group(1)) or (m.group(2) or ""), text)


def secret_values(tool: ToolDef) -> list[str]:
    """오류 메시지에 섞여 나가면 안 되는 값(이 도구가 쓰는 환경변수의 실제 값)."""
    return [v for n in env_names(tool) if (v := os.getenv(n)) and len(v) >= 6]


# ---------------------------------------------------------------- 모델에게 보이는 모양

def json_schema(tool: ToolDef) -> dict:
    props = {}
    for p in tool.params:
        s: dict = {"type": p.type}
        if p.description:
            s["description"] = p.description
        if p.enum:
            s["enum"] = p.enum
        if p.minimum is not None:
            s["minimum"] = p.minimum
        if p.maximum is not None:
            s["maximum"] = p.maximum
        props[p.name] = s
    return {"type": "object", "properties": props, "required": [p.name for p in tool.params if p.required], "additionalProperties": False}


def spec(tool: ToolDef) -> dict:
    """세 공급사 어댑터가 각자의 형식으로 바꾸는 공통 모양."""
    return {"name": tool.id, "description": tool.description, "schema": json_schema(tool)}
