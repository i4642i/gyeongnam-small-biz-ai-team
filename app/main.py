import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from app.agents import store
from app.offices import api as office_flow_api, flow as office_flow, store as office_store
from app.chat import history, llm, pipeline, providers
from app.runs import api as runs_api, service as runs_service
from app.tools import api as tools_api
from app.libraries import api as libraries_api, collect as libraries_collect
from app.rag import api as rag_api, graph_api as rag_graph_api, store as rag_store, worker as rag_worker
from app.config import BASE_DIR, LLM_MODE

logging.basicConfig(level=logging.INFO)

class ModelSetting(BaseModel):
    """이 캐릭터가 쓸 LLM. model 이 비어 있으면 공급사의 기본 모델로 채운다."""

    # 공급사는 providers.PROVIDERS 에서 자동으로 받는다(Literal 로 하드코딩하지 않는다 — 공급사를 추가할 때
    # 여기까지 같이 고치는 걸 잊어 저장이 422 로 막히는 일을 없애려고, 2026-09-25 deepseek 추가 계기).
    provider: str = "anthropic"
    model: str = Field(default="", max_length=80, pattern=r"^[A-Za-z0-9._:/@-]*$")

    @field_validator("provider")
    @classmethod
    def _provider_known(cls, v):
        if v not in providers.PROVIDERS:
            raise ValueError(f"공급사는 {', '.join(providers.PROVIDERS)} 중 하나여야 합니다.")
        return v


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    report_office_id: str | None = None   # 「내가 쓴 보고서」를 문맥으로 붙일 실행(없으면 붙이지 않는다)
    report_flow_id: str | None = None


log = logging.getLogger("agent_town")
app = FastAPI(title="경남 소상공인 상권 진단 AI 에이전트 팀", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(rag_api.router)
app.include_router(tools_api.router)
app.include_router(rag_graph_api.router)
app.include_router(runs_api.router)
app.include_router(office_flow_api.router)
app.include_router(libraries_api.router)
from app import regions_api  # noqa: E402  (의뢰 지역 목록)
app.include_router(regions_api.router)


@app.on_event("startup")
def _resume_documents():
    # 처리가 끝나지 않은 문서를 다시 줄 세운다. 서버가 뜨는 걸 막지 않도록 별도 스레드에서.
    import threading

    threading.Thread(target=rag_worker.recover, name="rag-recover", daemon=True).start()
    threading.Thread(target=libraries_collect.recover, name="collect-recover", daemon=True).start()   # 회사 컴퓨터의 끝나지 않은 수집 작업
    runs_service.recover()   # 서버가 꺼지면서 끝나지 못한 업무 실행은 '중단됨'으로 표시
    office_flow.recover()    # 끝나지 못한 사무실 흐름도 '중단됨'으로


def _get_or_404(agent_id: str) -> dict:
    try:
        return store.get(agent_id)
    except store.AgentNotFound:
        raise HTTPException(404, "에이전트를 찾을 수 없습니다")


@app.get("/api/health")
def health():
    return {"ok": True, "llm_mode": LLM_MODE}


# ---------- LLM 공급사 ----------

@app.get("/api/llm/providers")
def llm_providers():
    """모델 탭에서 고를 공급사 목록. 키가 설정됐는지(configured)만 알려 주고 키 자체는 절대 내보내지 않는다."""
    def describe(pid: str) -> dict:
        p = providers.PROVIDERS[pid]
        configured = providers.is_configured(pid)
        models, source = providers.list_models(pid)
        return {
            "id": pid, "label": p["label"], "configured": configured, "key_hint": providers.key_hint(pid),
            "default_model": p["default_model"], "models": models,
            "models_source": source,                              # live: 공급사에서 받은 최신 목록 / builtin: 내장 목록
            "models_checked": providers.BUILTIN_MODELS_CHECKED,   # 내장 목록을 공식 문서에서 확인한 날짜
        }
    with ThreadPoolExecutor(len(providers.PROVIDERS)) as pool:   # 공급사별 모델 목록 조회를 동시에
        return list(pool.map(describe, providers.PROVIDERS))


# ---------- 에이전트 ----------

def _clean_model(m: ModelSetting) -> dict:
    model = m.model.strip() or providers.PROVIDERS[m.provider]["default_model"]
    if not model:
        raise HTTPException(422, f"{providers.PROVIDERS[m.provider]['label']}의 모델 이름을 입력해 주세요")
    return {"provider": m.provider, "model": model}


@app.put("/api/agents/{agent_id}/model")
def update_agent_model(agent_id: str, body: ModelSetting):
    """직원이 쓸 LLM(공급사·모델)만 바꾼다(2026-10-01, 직원 소개 화면의 모델 탭). 이름·지시문·도구·대화 기록은 그대로다.
    API 키가 없는 공급사는 거절한다 — 골라 놓고 나중에 오류가 나는 일을 막는다."""
    _get_or_404(agent_id)
    if body.provider not in providers.PROVIDERS:
        raise HTTPException(422, "알 수 없는 공급사입니다")
    if not providers.is_configured(body.provider):
        raise HTTPException(409, f"{providers.PROVIDERS[body.provider]['label']}의 API 키가 서버 .env 에 없어 고를 수 없습니다")
    m = _clean_model(body)
    d = store.agent_dir(agent_id)
    store._write_json(d / "model.json", m)
    return {"ok": True, "model": m}


def _with_docs(agent: dict) -> dict:
    return {**agent, "docs": rag_store.summary(agent["id"])}   # 전문자료 개수(전체/준비됨/처리 중/실패)


def _affiliations() -> dict[str, list[dict]]:
    """캐릭터 id → 소속 목록 [{office_id, office, department, desk}] — 사무실 책상 배치에서 모은다(2026-09-29).
    회사마다 한 자리씩 앉을 수 있게 될 것을 대비해 목록으로 둔다."""
    from app.offices import store as office_store
    out: dict[str, list[dict]] = {}
    for o in office_store.list_all():
        depts = {d["id"]: d["name"] for d in o.get("departments", [])}
        for key, agent_id in (o.get("assignments") or {}).items():
            did, _, n = key.partition(":")
            out.setdefault(agent_id, []).append({"office_id": o["id"], "office": o.get("name", ""),
                                                 "department": depts.get(did, did), "desk": int(n) + 1 if n.isdigit() else None})
    return out


@app.get("/api/agents")
def list_agents():
    aff = _affiliations()
    return [{**_with_docs(a), "affiliations": aff.get(a["id"], [])} for a in store.list_all()]


@app.get("/api/agents/{agent_id}")
def get_agent(agent_id: str):
    return _with_docs(_get_or_404(agent_id))


# ---------- 대화 ----------
# def(동기) 엔드포인트: FastAPI가 스레드풀에서 실행하므로 LLM 호출이 서버 전체를 막지 않는다.

@app.get("/api/agents/{agent_id}/messages")
def list_messages(agent_id: str):
    _get_or_404(agent_id)
    return history.list_messages(agent_id)


@app.post("/api/agents/{agent_id}/messages")
def send_message(agent_id: str, body: MessageCreate):
    agent = _get_or_404(agent_id)
    text = body.content.strip()
    if not text:
        raise HTTPException(422, "메시지가 비어 있습니다")

    past = [{"role": m["role"], "content": m["content"]} for m in history.list_messages(agent_id)]
    messages = past + [{"role": "user", "content": text}]
    retrieval = messages   # 자료 검색어는 보고서 문맥 없이 대화만으로 만든다
    if body.report_flow_id and body.report_office_id:   # 임시 문맥: 대화 기록에는 저장하지 않고 요청마다 앞에 붙인다
        from app.offices import report_context
        try:
            ctx = report_context.build(body.report_office_id, body.report_flow_id, agent_id)
        except Exception:
            log.exception("보고서 문맥 만들기 실패 (%s)", body.report_flow_id)
            ctx = ""
        if ctx:
            messages = [{"role": "user", "content": ctx}, {"role": "assistant", "content": "네, 제가 쓴 보고서를 확인했습니다. 이 내용을 기준으로 답하겠습니다."}] + messages
    try:
        res = pipeline.answer(agent_id, agent, messages, mode="chat", retrieval_messages=retrieval, extra_tools=(["page_fetch"] if len(messages) > len(retrieval) else None))   # 보고서를 붙인 대화에서만 기사 원문 읽기   # 자료 검색 → 그래프 보강 → 도구 → 모델 (업무 실행과 같은 경로)
    except llm.ChatError as e:
        raise HTTPException(502, str(e))
    answer, sources, notice = res["answer"], res["sources"], res["notice"]
    saved = history.add_exchange(agent_id, text, answer, sources)
    return {**saved, "notice": notice} if notice else saved


@app.delete("/api/agents/{agent_id}/messages")
def clear_messages(agent_id: str):
    _get_or_404(agent_id)
    history.clear(agent_id)
    return {"ok": True}


# ---------- 사무실 ----------

@app.get("/api/offices")
def list_offices():
    return office_store.list_all()


@app.get("/api/offices/{office_id}")
def get_office(office_id: str):
    try:
        return office_store.get(office_id)
    except office_store.OfficeNotFound:
        raise HTTPException(404, "사무실을 찾을 수 없습니다")


class _NoCacheStatic(StaticFiles):
    """정적 파일(JS·CSS·HTML)에 no-cache 를 붙여, 배포 후 하드 새로고침 없이 일반 새로고침(F5)만으로도
    브라우저가 항상 최신본을 받게 한다. no-cache 는 '캐시를 쓰되 매번 서버에 확인(ETag)하라'는 뜻이라
    안 바뀐 파일은 304 로 가볍게 넘어간다(대역폭 낭비 없음)."""
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


app.mount("/", _NoCacheStatic(directory=BASE_DIR / "static", html=True), name="static")
