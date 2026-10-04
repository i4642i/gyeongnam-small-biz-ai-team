"""직원 대화에 붙이는 「내가 쓴 보고서」 문맥(2026-10-01).

사무실의 실행 기록에서, 이 직원이 쓴 결과(책상 출력)만 골라 대화 문맥으로 만든다.
대화 기록(history)에는 저장하지 않는다 — 요청마다 붙이고 끝나면 사라지는 임시 자료다(색인·전문자료 변경 없음).
"""

from __future__ import annotations

import json
import re

from app.offices import flow

OFFICE_ID = re.compile(r"^o_[0-9a-f]{8}$")
MAX_CHARS = 24000


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items() if not str(k).startswith("_")}
    if isinstance(o, list):
        return [_clean(x) for x in o]
    return o


def build(office_id: str, flow_id: str, agent_id: str) -> str:
    """이 직원이 그 실행에서 쓴 보고서를 문맥 글로. 쓴 것이 없으면 빈 문자열."""
    if not OFFICE_ID.match(office_id or ""):
        return ""
    f = flow.get(office_id, flow_id)
    outs = f.get("outputs") or {}
    mine = []
    for dept in f.get("departments") or []:
        for d in dept.get("desks") or []:
            if d.get("agent_id") == agent_id and d.get("key") in outs:
                mine.append((dept.get("name", ""), d.get("task_name", ""), outs[d["key"]]))
    if not mine:
        return ""
    inp = f.get("inputs") or {}
    head = (f"[참고 자료 — 내가 이번 의뢰에서 작성한 보고서]\n"
            f"의뢰: 지역 {inp.get('region', '')} · 구역 {inp.get('area') or '전체'} · 업종 {inp.get('upjong') or '전체'} · "
            f"관측 {inp.get('window_start', '')} ~ {inp.get('window_end', '')}\n"
            "아래는 내가 직접 쓴 결과입니다. 사용자가 이 보고서나 여기에 나온 기사·동네·점수에 대해 물으면 이 내용을 근거로 답하세요.\n"
            "말투: 지금은 보고서를 쓰는 때가 아니라 사용자와 1:1로 이야기하는 때입니다. 제목·번호 목록·굵은 글씨(별표)·표 같은 보고서 양식을 쓰지 말고, "
            "동료에게 설명하듯 자연스러운 문장으로 답하세요.\n"
            "기사 내용을 더 자세히 물으면 아래 대표 기사의 주소(url)를 page_fetch 도구로 열어 본문을 읽고 답하세요. 읽지 못했으면 읽지 못했다고 말하고, "
            "여기 없는 사실은 지어내지 마세요. 사용자가 어느 동네·기사인지 정확히 말하지 않아도 이 보고서에서 가장 알맞은 것을 골라 답하세요.\n")
    body = "\n\n".join(f"## {t or n}\n" + json.dumps(_clean(o), ensure_ascii=False, indent=1) for n, t, o in mine)
    text = head + "\n" + body
    return text[:MAX_CHARS]
