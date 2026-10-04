"""답변 만들기 공통 경로: 전문자료 검색 → 지식 그래프 보강 → 쓸 수 있는 도구 고르기 → 모델 호출 → 화면에 남길 출처.

대화(`POST /messages`)와 업무 실행(`app/runs`)이 같은 경로를 쓴다. mode 는 "chat"(대화) 또는 "task"(업무 실행: 도구 호출 한도가 더 넉넉하다).
"""

import logging

from app.chat import llm
from app.chat.providers import Truncated
from app.rag import graph_rag, search as rag_search, store as rag_store
from app.tools import registry as tools_registry
from app.rag.scope import from_inputs

log = logging.getLogger("agent_town")


def answer(agent_id: str, agent: dict, messages: list[dict], mode: str = "chat", retrieval_messages: list[dict] | None = None,
           inputs: dict | None = None, max_tokens: int | None = None, required_tools: list[str] | None = None, cancel_run_id: str | None = None,
           use_tools: bool = True, search_budget: int | None = None, extra_tools: list[str] | None = None) -> dict:
    """{"answer", "sources", "notice"}. 모델 오류는 llm.ChatError 로 올라간다.

    retrieval_messages: 자료를 찾을 때 볼 대화(기본은 messages). 검사 실패로 다시 시킬 때처럼 뒤에 덧붙은 지시문이 검색어를 흐리지 않게 한다.
    inputs: 업무 실행의 입력값. 모델이 도구 인자에 정확히 「@입력이름」 을 쓰면 도구를 부르기 전에 그 입력값으로 바꿔 넣는다(R5-1).
    max_tokens: 이 업무 전용 출력 한도(tasks.json 의 max_output_tokens) — 없으면 공급사 기본값. Claude 전용(다른 공급사는 무시).
    """
    src = retrieval_messages if retrieval_messages is not None else messages
    hits, notice, aug = [], None, {"extra": [], "info": [], "entities": [], "semantic": []}
    required = set(required_tools or []) if (mode == "task" and use_tools) else set()
    for tid in required:
        tool = tools_registry.get(tid)
        if tid not in agent.get("tools", []) or tool is None:
            raise llm.ChatError(f"판단 불가: 필수 도구 '{tid}'가 등록·활성화되어 있지 않습니다.")
        if tools_registry.env_missing(tool):
            raise llm.ChatError(f"판단 불가: 필수 도구 '{tid}'의 인증 설정이 없습니다.")
    try:
        scope = from_inputs(inputs, agent_id, "own") if mode == "task" else {}
        scope_pc = from_inputs(inputs, agent_id, "computer") if mode == "task" else {}
    except (ValueError, TypeError) as e:
        raise llm.ChatError(f"판단 불가: 검색 범위 입력 오류 — {e}") from e
    requires_rag = any(tools_registry.get(t).format == "rag_search" for t in required)
    if requires_rag and not rag_store.doc_ids_matching(agent_id, **scope):
        raise llm.ChatError("판단 불가: 종목·문서 종류·기준일에 맞는 전문자료가 없습니다. 게재일 미상 문서는 기준일 검색에서 제외합니다.")
    if any(tools_registry.get(t).format == "office_computer" for t in required):
        from app.libraries import access
        computers = access.computers_for(agent_id)
        if not computers:
            raise llm.ChatError("판단 불가: 이 직원이 앉은 회사에 놓인 컴퓨터가 없습니다.")
        if not any(rag_store.doc_ids_matching(c["id"], **scope_pc) for c in computers):
            raise llm.ChatError("판단 불가: 회사 컴퓨터에 종목·문서 종류·기준일에 맞는 자료가 없습니다. 게재일 미상 문서는 기준일 검색에서 제외합니다.")
    if rag_store.has_ready(agent_id):
        query = rag_search.make_query([m["content"] for m in src if m["role"] == "user"])
        try:
            hits = rag_search.retrieve(agent_id, query, **scope)
        except Exception:
            log.exception("전문자료 검색 실패 (%s)", agent_id)
            if mode == "task":
                raise llm.ChatError("판단 불가: 전문자료 검색에 실패했습니다. 자료 없이 보고서를 작성하지 않았습니다.")
            notice = "전문자료 검색에 실패해 자료 없이 답했습니다."
        # 지식 그래프가 있으면 질문에 나온 개체의 분류·관계와, 일반 검색이 놓친 관련 조각을 더한다(끌 수 있음). 실패해도 일반 답변은 이어간다.
        if not scope and graph_rag.enabled(agent_id):
            try:
                aug = graph_rag.augment(agent_id, query, hits)
            except Exception:
                log.exception("지식 그래프 보강 실패 (%s)", agent_id)
                notice = (notice + " " if notice else "") + "지식 그래프 보강에 실패해 일반 검색 결과로만 답했습니다."
        for i, h in enumerate(aug["extra"], start=len(hits) + 1):
            h["n"] = i
        if scope:
            notice = "전문자료 검색 범위: " + ", ".join(f"{k}={v}" for k, v in scope.items())
            if scope.get("as_of"):
                notice += ". 게재일 미상·기준일 이후 문서는 제외했습니다."
            notice += " 범위 필터를 지원하지 않는 지식 그래프 보강은 사용하지 않았습니다."
    all_hits = hits + aug["extra"]

    # 이 캐릭터가 쓰도록 고른 도구 중, 지금 실제로 쓸 수 있는 것(필요한 키가 있는 것)만 모델에게 준다. 못 쓰는 도구가 있으면 알린다.
    # use_tools=False: 'JSON 형식만 다시' 재시도 — 도구를 아예 주지 않아 재검색을 막고, 이미 쓴 초안에서 JSON 만 고치게 한다.
    usable, trace = [], []
    # extra_tools: 이 호출에서만 더 주는 도구(예: 대화에서 「내가 쓴 보고서」를 붙였을 때의 page_fetch). 직원 설정은 바꾸지 않으므로 업무 실행에는 영향이 없다.
    base_tools = list(agent.get("tools", [])) + [t for t in (extra_tools or []) if t not in agent.get("tools", [])]
    for tid in (base_tools if use_tools else []):
        t = tools_registry.get(tid)
        if t is None:
            continue
        missing = tools_registry.env_missing(t)
        if missing:
            notice = (notice + " " if notice else "") + f"'{t.label or t.id}' 도구를 쓰려면 서버의 .env에 {', '.join(missing)}가 필요해서 도구 없이 답했습니다."
        else:
            usable.append(t)
    history = None   # 재시도가 이어받을 대화(도구 결과 포함) — 도구를 쓴 실행에서만 채워진다(#1: 관측 유실 방지)
    if usable:
        try:
            text, trace, history = llm.reply_with_tools(agent, messages, all_hits, aug["info"], usable, agent_id, mode, inputs, max_tokens, cancel_run_id, search_budget)
        except Truncated as e:
            # 이어쓰기에도 최초 검색 근거와 잘리지 않은 계산 원본을 보존한다.
            e.trace = [{"n": h["n"], "doc_id": h["doc_id"], "filename": h["filename"],
                        "page": h["page"], "snippet": h["text"][:500], "via": h.get("via", "search"), "published_at": h.get("published_at")}
                       for h in all_hits] + [{**s, "via": "tool"} for s in e.trace]
            raise
    else:
        text = llm.reply(agent, messages, all_hits, aug["info"])   # 프롬프트 로그는 llm.reply 안에서 남긴다(중복 빌드 방지)

    # 화면에 보여 줄 출처: 조각 전체가 아니라 앞부분만 (전체는 검색 시험에서 볼 수 있다)
    sources = [{"n": h["n"], "doc_id": h["doc_id"], "filename": h["filename"], "page": h["page"],
                "snippet": h["text"][:500], "sim": h["sim"], "via": h.get("via", "search"), "published_at": h.get("published_at")} for h in all_hits]
    if aug["info"]:   # 프롬프트에 넣은 그래프 정보도 기록해서 화면에서 확인할 수 있게 한다
        sources.append({"n": None, "via": "graph-info", "doc_id": None, "filename": "지식 그래프", "page": None, "sim": None,
                        "snippet": "\n".join(aug["info"])[:1800], "entities": aug["entities"], "semantic": aug.get("semantic", [])})
    for entry in trace:   # 도구 사용 기록(어떤 검색어로 무엇을 받았는지)도 같은 곳에 저장해 답변 아래에서 펼쳐 볼 수 있게 한다
        sources.append({"n": None, "via": "tool", "doc_id": None, "filename": entry["label"], "page": None, "sim": None, **entry})
    return {"answer": text, "sources": sources, "notice": notice, "history": history}
