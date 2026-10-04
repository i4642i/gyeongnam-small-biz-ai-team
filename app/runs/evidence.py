"""Checks against execution-owned evidence, never against model-supplied hashes alone."""

from app.runs import checks
from app.tools import registry

RAG_FORMATS = ("rag_search", "office_computer")   # 문서 조각을 근거로 돌려주는 도구(캐릭터 문서함·회사 컴퓨터)


def problems(task: dict, text: str, sources: list[dict]) -> list[str]:
    required = task.get("required_tools") or []
    needs_rag = any((t := registry.get(tid)) is not None and t.format in RAG_FORMATS for tid in required)
    errors = []
    for tid in required:
        attempts = [s for s in sources if s.get("tool") == tid]
        if attempts and not any(s.get("ok") for s in attempts):
            errors.append(f"판단 불가: 필수 도구 '{tid}' 호출이 모두 실패했습니다.")
    if needs_rag:
        has_rag = any(s.get("doc_id") is not None and s.get("snippet") for s in sources)
        has_rag_tool = any(s.get("ok") and s.get("items") and
                           (t := registry.get(s.get("tool", ""))) is not None and t.format in RAG_FORMATS
                           for s in sources)
        if not (has_rag or has_rag_tool):
            errors.append("판단 불가: 필수 전문자료에서 사용할 근거를 얻지 못했습니다.")

    data, _, _ = checks.extract_json(text)
    is_portfolio = "pm_build_portfolio" in required or (isinstance(data, dict) and data.get("report") == "portfolio")
    if not is_portfolio:
        return errors
    calls = [s for s in sources if s.get("tool") == "pm_build_portfolio" and s.get("ok")]
    original = calls[-1].get("server_result") if calls else None
    if not isinstance(original, dict) or not calls[-1].get("server_inputs_bound"):
        return errors + ["판단 불가: 같은 실행의 포트폴리오 계산 원본이 없습니다. pm_build_portfolio를 호출해야 합니다."]
    if not isinstance(data, dict):
        return errors + ["서버 계산 결과 대조 불가: 포트폴리오 JSON 객체가 없습니다."]
    for key in ("config_version", "excluded", "cash_pct", "sector_weights", "review", "portfolio_hash"):
        if key not in original or key not in data or data[key] != original[key]:
            errors.append(f"서버 계산 결과 불일치: {key}")
    expected, actual = original.get("positions"), data.get("positions")
    if not isinstance(expected, list) or not isinstance(actual, list) or len(expected) != len(actual):
        return errors + ["서버 계산 결과 불일치: positions 종목 수"]
    for i, (want, have) in enumerate(zip(expected, actual)):
        # 계산 필드는 모두 대조하되, 분석가가 추가하는 thesis/exit_conditions는 허용한다.
        if not isinstance(want, dict) or not isinstance(have, dict):
            errors.append(f"서버 계산 결과 불일치: positions[{i}]")
            continue
        for key, value in want.items():
            if key not in have or have[key] != value:
                errors.append(f"서버 계산 결과 불일치: positions[{i}].{key}")
    return errors
