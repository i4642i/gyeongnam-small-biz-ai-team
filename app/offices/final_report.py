"""4부: 최종 보고서 — 운용팀(⑧) 실행이 끝나면 후보 포트폴리오를 사람이 읽을 한 장짜리 MD 로 코드로 묶는다.
모델을 부르지 않는다(순수 조립). flow["outputs"] 안의 JSON 을 report 값(head/verify/peer/portfolio)으로 찾아
종목별 요약(⑤ 선정 근거 + ⑥ 검증 + ⑦ 비교 + 무효화 상태), 제외 종목, ⑧ 의 충돌, 참고 표, 면책 문구를 만든다.

부서·책상 id 는 사무실마다 다르므로 하드코딩하지 않고, 각 JSON 의 "report" 상수 값으로 찾는다
(reporter 4종은 head 를 만드는 데만 쓰이고 최종 보고서 자체에는 인용하지 않는다 — head 가 이미 요약해 담고 있다)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _all_json_blobs(flow: dict) -> list[dict]:
    """flow["outputs"] 안의 모든 JSON 객체를 평평하게 모은다 — 일반 부서 결과(책상 키)와
    반복 부서 결과(부서 id → {covered, not_covered, items:[{result}]}) 둘 다 훑는다."""
    out: list[dict] = []
    for v in (flow.get("outputs") or {}).values():
        if not isinstance(v, dict):
            continue
        if "report" in v:
            out.append(v)
        elif isinstance(v.get("items"), list):   # 반복 부서 집계
            for it in v["items"]:
                r = it.get("result") if isinstance(it, dict) else None
                if isinstance(r, dict) and "report" in r:
                    out.append(r)
    return out


def _by_report(blobs: list[dict], name: str) -> list[dict]:
    return [b for b in blobs if b.get("report") == name]


def _fmt_pct(x) -> str:
    # 소수 둘째 자리 그대로(2026-09-28, 숙제 E8) — 예전 첫째 자리 반올림은 18.75→18.8, 6.25→6.2 로 합계가 100% 가 안 됐다
    return f"{x:.2f}%" if isinstance(x, (int, float)) else "—"


GICS_SECTOR = {"10": "에너지", "15": "소재", "20": "산업재", "25": "경기소비재", "30": "필수소비재", "35": "헬스케어",
               "40": "금융", "45": "IT", "50": "커뮤니케이션", "55": "유틸리티", "60": "부동산"}


def _sector(code) -> str:
    c = str(code or "")[:2]
    return f"{GICS_SECTOR[c]}({c})" if c in GICS_SECTOR else c


def build(flow: dict) -> str:
    """완료된(운용팀까지 성공한) 흐름 하나에서 최종 보고서 MD 문자열을 만든다.
    운용팀 결과가 없으면(아직 안 끝났거나 실패) 빈 문자열을 돌려준다 — 호출 쪽에서 빈 값이면 붙이지 않는다."""
    blobs = _all_json_blobs(flow)
    heads = _by_report(blobs, "head")
    verifies = _by_report(blobs, "verify")
    peers = _by_report(blobs, "peer")
    portfolios = _by_report(blobs, "portfolio")
    if not portfolios:
        picks = _by_report(blobs, "news_picks") or _by_report(blobs, "news_picks_kr")   # 국내판(2026-09-29)도 같은 조립을 쓴다
        if picks:
            return _build_news(flow, picks[0])
        district_picks = _by_report(blobs, "changwon_picks")   # 지역 상권 회사(2026-09-29, 창원에서 시작) — 종목이 아니라 동네가 후보
        return _build_district(flow, district_picks[0]) if district_picks else ""
    head = heads[0] if heads else {}
    pm = portfolios[0]
    v_by_ticker = {v.get("ticker"): v for v in verifies}
    p_by_ticker = {p.get("ticker"): p for p in peers}
    cand_by_ticker = {c.get("ticker"): c for c in (head.get("candidates") or [])}

    lines: list[str] = []
    now = datetime.now(timezone.utc).isoformat()
    lines.append(f"# 최종 보고서 — {flow.get('office_name', '')} ({flow.get('inputs', {}).get('window_end', '')})")
    lines.append("")
    lines.append(f"작성 시각(UTC): {now}  ·  흐름 id: `{flow.get('id')}`")
    lines.append("")
    lines.append(f"> {pm.get('notice') or '참고용 제안이며 투자 결정은 대표님이 합니다.'}")
    if head.get("_seeded_from_run"):
        lines.append(f"> ⚠ 팀장 결과(⑤)는 새로 실행하지 않고 이전 실행 `{head['_seeded_from_run']}` 을 그대로 재사용했습니다.")
    lines.append("")

    # A. 요약
    lines.append("## A. 요약")
    lines.append("")
    lines.append(pm.get("summary_ko") or pm.get("summary") or "(요약 없음)")
    lines.append("")

    # B. 후보 포트폴리오
    lines.append("## B. 후보 포트폴리오")
    lines.append("")
    # 감시 신호는 전부 보이고 무효화된 것만 표시한다(예전엔 무효화된 것만 적어 평소엔 칸이 늘 '—'였다, 숙제 E8)
    lines.append("| 티커 | 종목명 | 섹터 | 비중 | 확신도 | 검증 | 동종비교 | 감시 신호(무효화 표시) | 핵심 청산 조건 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for p in sorted(pm.get("positions") or [], key=lambda x: -(x.get("weight_pct") or 0)):
        t = p.get("ticker")
        sigs = list(p.get("monitor_signals") or [])
        exits = p.get("exit_conditions") or []
        first_exit = (str(exits[0])[:70] + ("…" if len(str(exits[0])) > 70 else "")) if exits else "—"
        lines.append(f"| {t} | {p.get('name') or ''} | {_sector(p.get('gics_sector', ''))} | {_fmt_pct(p.get('weight_pct'))} | "
                      f"{p.get('conviction', '')} | {p.get('verify_verdict', '')} | {p.get('peer_verdict', '')} | "
                      f"{', '.join(sigs) or '—'} | {first_exit.replace('|', '/')} |")
    lines.append(f"| **현금** |  |  | **{_fmt_pct(pm.get('cash_pct'))}** |  |  |  |  |  |")
    lines.append("")
    lines.append(f"지문(portfolio_hash): `{pm.get('portfolio_hash', '')}`  ·  설정 버전: `{pm.get('config_version', '')}`")
    lines.append("")

    # C. 종목별 요약 (⑤+⑥+⑦ 결합)
    lines.append("## C. 종목별 요약")
    lines.append("")
    for p in pm.get("positions") or []:
        t = p.get("ticker")
        c, v, pe = cand_by_ticker.get(t, {}), v_by_ticker.get(t), p_by_ticker.get(t)
        lines.append(f"### {t} — {p.get('name') or ''}")
        lines.append("")
        lines.append(f"- **선정 근거(⑤)**: {c.get('hypothesis') or c.get('thesis') or '(원문 없음)'}")
        if v:
            lines.append(f"- **기업 검증(⑥)**: {v.get('overall', {}).get('verdict')} — {v.get('overall', {}).get('basis', '')}")
            for rf in v.get("red_flags") or []:
                lines.append(f"  - 위험 신호({rf.get('severity')}): {rf.get('issue')}")
        else:
            lines.append("- **기업 검증(⑥)**: 자료 없음")
        if pe:
            lines.append(f"- **동종 비교(⑦)**: {pe.get('overall', {}).get('verdict')} — {pe.get('overall', {}).get('basis', '')}")
        else:
            lines.append("- **동종 비교(⑦)**: 자료 없음")
        lines.append(f"- **운용 논리(⑧)**: {p.get('thesis', '')}")
        for ec in p.get("exit_conditions") or []:
            lines.append(f"  - 청산 조건: {ec}")
        lines.append("")

    # D. 제외 종목
    lines.append("## D. 제외 종목")
    lines.append("")
    excluded = pm.get("excluded") or []
    if excluded:
        lines.append("| 티커 | 자동/판단 | 사유 |")
        lines.append("|---|---|---|")
        for e in excluded:
            lines.append(f"| {e.get('ticker')} | {'자동' if e.get('auto') else '운용 매니저 판단'} | {e.get('reason', '')} |")
    else:
        lines.append("(제외된 후보 없음)")
    lines.append("")

    # E. 충돌
    lines.append("## E. 분석가 간 충돌")
    lines.append("")
    conflicts = pm.get("conflicts") or []
    if conflicts:
        for cf in conflicts:
            lines.append(f"- **{cf.get('ticker')}**: {cf.get('issue')} → {cf.get('resolution')}")
    else:
        lines.append("(기록된 충돌 없음)")
    lines.append("")

    # F. 참고 표 — 검증에서 못 다룬(자료 없음) 후보, 동종 비교 못 다룬 후보
    lines.append("## F. 참고 — 처리 현황")
    lines.append("")
    all_cands = {c.get("ticker") for c in (head.get("candidates") or [])}
    lines.append(f"- 팀장 후보: {len(all_cands)}개  ·  검증 완료: {len(v_by_ticker)}개  ·  동종 비교 완료: {len(p_by_ticker)}개  ·  편입: {len(pm.get('positions') or [])}개")
    missing_verify = sorted(all_cands - set(v_by_ticker))
    missing_peer = sorted(all_cands - set(p_by_ticker))
    if missing_verify:
        lines.append(f"- 검증 자료 없음: {', '.join(missing_verify)}")
    if missing_peer:
        lines.append(f"- 동종 비교 자료 없음: {', '.join(missing_peer)}")
    for dep in flow.get("departments") or []:
        for desk in dep.get("desks") or []:
            if desk.get("status") == "not_covered":
                reason = str(desk.get("error") or "사유 미기록").replace("\n", " ")
                lines.append(f"- 판단 불가 / 미처리 — {dep.get('name', '')}, 항목 {desk.get('repeat_index', '?')}: {reason}")
    lines.append("")

    lines.append("---")
    lines.append(pm.get("review") or "")
    lines.append("")
    lines.append(f"*{pm.get('notice') or '참고용 제안이며 투자 결정은 대표님이 합니다.'}*")
    return "\n".join(lines)


# ---------------------------------------------------------------- 뉴스 회사(2026-09-29): 편집장 결과로 조립

def _editor_prose(flow: dict, blob: dict) -> str:
    """편집장 책상의 실행 본문에서 끝의 고정 JSON 을 뺀 자유 보고서 부분."""
    from app.runs import checks, service as runs
    for key, v in (flow.get("outputs") or {}).items():
        if v is not blob:
            continue
        for dep in flow.get("departments") or []:
            for x in dep.get("desks") or []:
                if x.get("key") == key and x.get("run_id"):
                    try:
                        text = runs.get(x["agent_id"], x["run_id"]).get("answer") or ""
                    except Exception:
                        return ""
                    _, start, _, _ = checks.locate_json(text)
                    if start is not None:
                        fence = text.rfind("```", 0, start)
                        text = text[:fence if fence != -1 else start]
                    return text.strip()
    return ""


def _build_news(flow: dict, pick: dict) -> str:
    """뉴스 회사 최종 보고서: 선정 10개 표 + 편집장의 자유 보고서 본문 + 빠진 후보. 모델을 부르지 않는다."""
    inputs = flow.get("inputs") or {}
    lines = [f"# 최종 보고서 — {flow.get('office_name', '')} ({inputs.get('window_start', '')} ~ {inputs.get('window_end', '')})", "",
             f"작성 시각(UTC): {datetime.now(timezone.utc).isoformat()}  ·  흐름 id: `{flow.get('id')}`", "",
             "> 화제성과 기업 체력을 함께 본 참고용 목록이며 매매 권유가 아닙니다. 투자 결정은 대표님이 합니다.", ""]
    if pick.get("headline"):
        lines += [f"**{pick['headline']}**", ""]
    lines += ["## 이번 주 10개", "", "| # | 종목 | 테마 | 화제 점수 | 체력 등급 | 한 줄 이유 | 대표 기사 |", "|---|---|---|---|---|---|---|"]
    for i, p in enumerate(pick.get("picks") or [], 1):
        art = p.get("key_article") or {}
        link = f"[{art.get('domain') or '기사'} {art.get('published') or ''}]({art.get('url')})" if art.get("url") else "—"
        cell = lambda v: str(v if v not in (None, "") else "—").replace("|", "/").replace(chr(10), " ")
        lines.append(f"| {i} | **{cell(p.get('ticker'))}** {cell(p.get('name'))} | {cell(p.get('theme'))} | {cell(p.get('buzz_score'))} | "
                     f"{cell(p.get('health_grade'))} | {cell(p.get('reason'))} | {link} |")
    lines.append("")
    prose = _editor_prose(flow, pick)
    if prose:
        lines += ["## 편집장 보고서", "", prose, ""]
    if pick.get("left_out"):
        lines += ["## 화제였지만 뺀 종목", "", "| 종목 | 뺀 이유 |", "|---|---|"]
        for x in pick["left_out"]:
            lines.append(f"| {x.get('ticker', '')} | {str(x.get('why', '')).replace('|', '/')} |")
        lines.append("")
    return chr(10).join(lines)


def _build_district(flow: dict, pick: dict) -> str:
    """지역 상권 회사 최종 보고서(2026-09-29, 창원): 선정 동네 표 + 편집장 본문. 종목 대신 동네·구가 단위다."""
    inputs = flow.get("inputs") or {}
    lines = [f"# 최종 보고서 — {flow.get('office_name', '')} ({inputs.get('window_start', '')} ~ {inputs.get('window_end', '')})", "",
             f"작성 시각(UTC): {datetime.now(timezone.utc).isoformat()}  ·  흐름 id: `{flow.get('id')}`", "",
             "> 화제와 상권 구성을 함께 본 참고용 정보이며 투자·창업 권유가 아닙니다. 결정은 직접 판단하셔야 합니다.", ""]
    if pick.get("headline"):
        lines += [f"**{pick['headline']}**", ""]
    lines += ["## 이번 주 상권", "", "| # | 동네 | 구 | 테마 | 상권 등급 | 사건 | 조언 | 대표 기사 |", "|---|---|---|---|---|---|---|---|"]
    cell = lambda v: str(v if v not in (None, "") else "—").replace("|", "/").replace(chr(10), " ")
    for i, p in enumerate(pick.get("picks") or [], 1):
        art = p.get("key_article") or {}
        link = f"[{art.get('domain') or '기사'} {art.get('published') or ''}]({art.get('url')})" if art.get("url") else "—"
        lines.append(f"| {i} | **{cell(p.get('area'))}** | {cell(p.get('district'))} | {cell(p.get('theme'))} | {cell(p.get('grade'))} | "
                     f"{cell(p.get('event'))} | {cell(p.get('advice'))} | {link} |")
    lines.append("")
    prose = _editor_prose(flow, pick)
    if prose:
        lines += ["## 편집장 보고서", "", prose, ""]
    return chr(10).join(lines)
