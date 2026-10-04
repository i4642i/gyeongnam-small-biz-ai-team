"""숫자 변수 방식(2026-09-28, 숙제 A′-3) — 모델은 숫자를 직접 쓰지 않고 변수로 가리키고, 서버가 확인·계산·채운다.

업무 설정 number_binding=true 인 업무(⑥ 기업 검증)에만 적용한다.

모델이 내는 것
- 결과 JSON 의 variables[]: 변수마다 원문 표기 그대로의 값(raw)·지표 이름(label, 원문 낱말)·단위(unit)·배수(scale)·
  기간 종류(period_type)·기간 끝(period_end)·범위(scope)·출처(source=evidence, evidence_id).
- 글(A·B 절과 JSON 의 설명 칸)에는 숫자 대신 {변수} 와 {chg:나중,이전}(증감·증감률·증가/감소 낱말)만.
- checks[].direction(선택): {"expected": "up"|"down", "from": 이전 변수, "to": 나중 변수} — 확인 항목이 방향을 물을 때.

서버가 하는 것
- problems(): 검사 단계. 값이 근거 조각에 글자 그대로 없거나, 같은 줄에 지표 이름이 없거나, 표 머리의 날짜·단위와
  어긋나면 오류(재요청). 날짜·단위 정보가 아예 없어 확인할 수 없으면 경고(warnings()). 문장 속 숫자 직접 쓰기,
  판정과 계산된 방향이 어긋남도 오류. 오류 앞에 [number] 를 붙인다 — 재요청은 하되, 재요청 뒤에도 남으면 실행을
  버리지 않고 〔확인 안 됨〕으로 채워 경고 통과(app/runs/soft.py all_soft(final=True)).
- apply(): 값 확정·단위 변환·억/만 표기·증감 계산 후 글 전체의 자리표시자를 채운다.

대조 대상은 인용문이 아니라 인용문이 든 색인 조각 전체다(2026-09-28 실측: 숫자는 조각엔 있는데 인용문을 짧게 잘라
인용문엔 없는 경우가 많았다). 조각은 표 머리(열 날짜)·단위 줄을 조각마다 반복한다(app/rag/chunking.py).
"""

from __future__ import annotations

import logging
import re
from contextlib import closing
from datetime import date

from app.runs import checks

log = logging.getLogger("agent_town.runs")

TAG = "[number]"
SCALE = {"one": 1.0, "thousand": 1e3, "million": 1e6, "billion": 1e9}
_PH = re.compile(r"\{(chg:)?([A-Z][A-Z0-9_]{1,40})(?:,([A-Z][A-Z0-9_]{1,40}))?\}")
_BROKEN_PH = re.compile(r"\{(?:chg\s*:\s*)?[A-Z][A-Z0-9_]{1,40}[^{}\n]{0,50}?\}|\{chg\s*:[^{}\n]{0,60}\}")
_NUM = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?")
_RAW_OK = re.compile(r"^\(?-?\d[\d,]*(?:\.\d+)?\)?$")
_STOP = {"the", "of", "a", "an", "and", "in", "for", "to", "on", "at", "net"}   # 'net' 은 따로 본다(아래 _label_ok)
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DATE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s*((?:19|20)\d{2})?", re.I)
_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
_UNITS = re.compile(r"in\s+(thousands|millions|billions)", re.I)
_TYPE_WORDS = {"3M": re.compile(r"three months|13 weeks|thirteen weeks|quarter", re.I),
               "6M": re.compile(r"six months|26 weeks|twenty-six weeks", re.I),
               "9M": re.compile(r"nine months|39 weeks|thirty-nine weeks", re.I),
               "12M": re.compile(r"twelve months|year ended|years ended|fiscal year", re.I)}
_TYPE_WORDS["FY"] = _TYPE_WORDS["12M"]
# JSON 설명 칸에 숫자를 직접 쓴 것 — 금액·비율·배수처럼 단위가 붙은 숫자만(연도·날짜·문서 이름은 안 잡는다)
_BARE = re.compile(r"(\$\s?\d[\d,.]*|\d[\d,]*(?:\.\d+)?\s?(?:조|억|만\s?달러|천\s?달러|백만|달러|million|billion|thousand|%p|%|bp|배|호)(?![a-z]))", re.I)
_TEXT_KEYS = ("issue", "finding", "actual", "summary_ko", "basis", "description", "flag", "detail")   # ⑥ 위험 신호 글은 issue
UNVERIFIED = "〔확인 안 됨〕"


# ---------------------------------------------------------------- 조각 읽기

def _load_chunks(run: dict) -> list[str]:
    """이번 실행에서 연 문서들의 조각 원문(정규화 전)."""
    from app.rag import store
    from app.runs import server_fill
    out: list[str] = []
    for owner, doc_id in server_fill._retrieved(run.get("sources") or [], run.get("agent_id") or ""):
        try:
            with closing(store._connect(owner)) as conn:
                out += [r["text"] for r in conn.execute("SELECT text FROM chunks WHERE doc_id = ? ORDER BY ord", (doc_id,))]
        except Exception:
            log.exception("숫자 대조용 문서를 읽지 못했습니다 — %s/%s", owner, doc_id)
    return out


def _chunk_of(quote: str, chunks: list[str]) -> str | None:
    from app.runs.server_fill import _norm
    q = _norm(quote)
    if len(q) < 8:
        return None
    for c in chunks:
        if q in _norm(c):
            return c
    return None


# ---------------------------------------------------------------- 값 하나 확인

def _digits(tok: str) -> str:
    return tok.replace(",", "").replace("(", "").replace(")", "").replace("-", "").strip()


def raw_value(raw: str) -> float | None:
    s = str(raw).strip().replace("$", "").strip()
    if not _RAW_OK.match(s):
        return None
    neg = s.startswith("(") or s.startswith("-")
    v = float(_digits(s))
    return -v if neg else v


def _label_ok(label: str, text: str) -> bool:
    """지표 이름(원문 낱말)의 낱말이 모두 그 줄(또는 문장)에 있는가. 'net' 은 이름에 있으면 줄에도 있어야 한다
    (net interest income ↔ interest on deposits 같은 오독을 막는 핵심 낱말)."""
    words = [w for w in re.findall(r"[a-z0-9&]+", label.lower()) if w not in _STOP or w == "net"]
    low = text.lower()
    return bool(words) and all(w in low for w in words)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_value_row(line: str) -> bool:
    """첫 칸에 이름이 있고 뒤 칸에 숫자(연도만 있는 칸 제외)가 있는 표 줄 — 머리 줄이 아니다."""
    c = _cells(line)
    if not c or not c[0]:
        return False
    return any(x and _RAW_OK.match(x.replace("$", "").strip()) and not re.fullmatch(r"(19|20)\d{2}", x.strip()) for x in c[1:])


def _header_text_for(lines: list[str], row_i: int, cell_i: int) -> str:
    """표 줄 row_i 의 cell_i 칸 위에 있는 머리 글(여러 머리 줄의 같은 열 — 가장 가까운 왼쪽의 비지 않은 칸)."""
    parts = []
    for l in lines[:row_i]:
        if not l.lstrip().startswith("|") or _is_value_row(l):   # 머리 줄만(위쪽 값 줄의 숫자가 섞이지 않게)
            continue
        c = _cells(l)
        for j in range(min(cell_i, len(c) - 1), -1, -1):
            if c[j] and not set(c[j]) <= set("-: "):
                if j > 0 or cell_i == 0:
                    parts.append(c[j])
                break
    return " ".join(parts)


def _dates(text: str) -> list[tuple[int, int, int | None]]:
    out = []
    for m in _DATE.finditer(text):
        out.append((_MONTHS[m.group(1).lower()[:3]], int(m.group(2)), int(m.group(3)) if m.group(3) else None))
    return out


def check_variable(v: dict, ev_by_id: dict, chunks: list[str]) -> tuple[list[str], list[str]]:
    """(오류, 경고). 오류는 재요청 대상."""
    vid = v.get("id", "?")
    errs, warns = [], []
    e = ev_by_id.get(v.get("evidence_id"))
    if not e:
        return [f"{vid}: evidence_id {v.get('evidence_id')} 가 evidence 에 없습니다."], warns
    val = raw_value(v.get("raw", ""))
    if val is None:
        return [f"{vid}: raw '{v.get('raw')}' 는 원문 숫자 표기 그대로여야 합니다(예: 202.7, 226,892, (1,157)) — 단위·글자 없이."], warns
    chunk = _chunk_of(e.get("quote", ""), chunks)
    if chunk is None:
        warns.append(f"{vid}: 근거 {e.get('evidence_id')} 의 인용문을 원문 조각에서 찾지 못해 숫자를 확인하지 못했습니다.")
        return errs, warns
    want = _digits(str(v["raw"]).replace("$", ""))
    lines = chunk.split("\n")
    hits = []   # (줄 번호, 칸 번호 또는 None, 글자 위치)
    for i, l in enumerate(lines):
        for m in _NUM.finditer(l):
            if _digits(m.group()) == want:
                cell_i = None
                if l.lstrip().startswith("|"):
                    cell_i = l[: m.start()].count("|") - 1
                hits.append((i, cell_i, m.start()))
    if not hits:
        return [f"{vid}: 값 {v['raw']} 가 근거 {e['evidence_id']} 의 원문 조각에 없습니다 — 조각에 실제로 있는 숫자를 원문 표기 그대로 적으세요."], warns
    label = str(v.get("label") or "")
    good = []
    for i, cell_i, pos in hits:
        l = lines[i]
        ctx = l if cell_i is not None else l[max(0, pos - 250): pos + 250]
        if _label_ok(label, ctx):
            good.append((i, cell_i, pos, ctx))
    if not good:
        return [f"{vid}: 근거 {e['evidence_id']} 에서 값 {v['raw']} 와 같은 줄(문장)에 지표 이름 '{label}' 이 없습니다 — "
                "다른 지표의 숫자를 옮겼는지 확인하세요. label 은 그 줄의 원문 낱말을 그대로(줄여도 되지만 바꾸거나 번역하지 말고) 적습니다."], warns
    # 기간·단위: 조각 안의 표 머리(표 줄) 또는 문장 주변(글)
    period_end = str(v.get("period_end") or "")
    ptype = str(v.get("period_type") or "")
    scale = str(v.get("scale") or "one")
    period_ok = unit_ok = False
    conflicts, unit_conflicts = [], []
    for i, cell_i, pos, ctx in good:
        head = _header_text_for(lines, i, cell_i) if cell_i is not None else lines[i][max(0, pos - 250): pos + 160]
        ds = _dates(head)
        if period_end and ds:
            try:
                pe = date.fromisoformat(period_end)
                same = [d for d in ds if d[0] == pe.month and abs(d[1] - pe.day) <= 3 and (d[2] in (None, pe.year))]
                years = {int(y) for y in _YEAR.findall(head)}
                if same and (pe.year in years or any(d[2] == pe.year for d in same)):
                    period_ok = True
                elif cell_i is not None:
                    conflicts.append(f"표 머리의 기간은 '{head[:80]}' 인데 period_end 가 {period_end}")
            except ValueError:
                pass
        if cell_i is not None and ptype in _TYPE_WORDS:
            other = [t for t, rx in _TYPE_WORDS.items() if t not in (ptype, "FY", "12M") and rx.search(head)]
            if other and not _TYPE_WORDS[ptype].search(head):
                conflicts.append(f"표 머리는 '{head[:80]}'(기간 종류 {other[0]}) 인데 period_type 이 {ptype}")
        # 단위: 글이면 숫자 바로 뒤의 million/billion/thousand, 표면 조각의 'in thousands/millions'
        after = lines[i][pos: pos + len(str(v['raw'])) + 14].lower()
        m = re.search(r"\b(thousand|million|billion)", after)
        found = (m.group(1) if m else None)
        if found is None and cell_i is not None:
            u = _UNITS.search(chunk)
            found = u.group(1).rstrip("s") if u else None
        if found:
            if found == scale:
                unit_ok = True
            elif v.get("unit") == "USD":
                unit_conflicts.append(f"원문 단위는 {found} 인데 scale 이 {scale}")
        if v.get("unit") in ("percent", "bp", "count", "ratio", "days", "other"):
            unit_ok = True
    bad = (conflicts if not period_ok else []) + (unit_conflicts if not unit_ok else [])
    if bad:
        errs.append(f"{vid}: " + "; ".join(sorted(set(bad))) + " — 원문 머리의 기간·단위와 맞게 고치세요.")
    if not period_ok and not conflicts:
        warns.append(f"{vid}: 근거 조각에서 기간({period_end})을 확인할 정보가 없습니다.")
    if not unit_ok and not unit_conflicts and v.get("unit") == "USD":
        warns.append(f"{vid}: 근거 조각에서 단위(scale={scale})를 확인할 정보가 없습니다.")
    return errs, warns


# ---------------------------------------------------------------- 결과 전체 검사

def _variables(data: dict) -> dict[str, dict]:
    """id → 변수(사본). 보고서의 ticker 를 _ticker 로 붙여 둔다 — 모델이 다른 회사를 가리키지 못하게."""
    return {v["id"]: {**v, "_ticker": data.get("ticker")}
            for v in (data.get("variables") or []) if isinstance(v, dict) and v.get("id")}


def _text_fields(data: dict):
    """(경로, 글) — 숫자를 직접 쓰면 안 되는 JSON 설명 칸."""
    for i, c in enumerate(data.get("checks") or [], 1):
        if isinstance(c, dict) and isinstance(c.get("finding"), str):
            yield f"checks[{i}].finding", c["finding"]
    ex = data.get("exposure_check") or {}
    if isinstance(ex.get("actual"), str):
        yield "exposure_check.actual", ex["actual"]
    for i, f in enumerate(data.get("red_flags") or [], 1):
        if isinstance(f, dict):
            for k in _TEXT_KEYS:
                if isinstance(f.get(k), str):
                    yield f"red_flags[{i}].{k}", f[k]
    if isinstance(data.get("summary_ko"), str):
        yield "summary_ko", data["summary_ko"]
    ov = data.get("overall") or {}
    if isinstance(ov.get("basis"), str):
        yield "overall.basis", ov["basis"]


def value_of(v: dict) -> float | None:
    val = raw_value(v.get("raw", ""))
    if val is None:
        return None
    return val * SCALE.get(str(v.get("scale") or "one"), 1.0)


def _verdict_problems(data: dict, vars_: dict) -> list[str]:
    out = []
    for i, c in enumerate(data.get("checks") or [], 1):
        d = c.get("direction") if isinstance(c, dict) else None
        if not isinstance(d, dict):
            continue
        a, b = vars_.get(d.get("to")), vars_.get(d.get("from"))
        if not a or not b:
            out.append(f"checks[{i}].direction: 변수 {d.get('from')}/{d.get('to')} 가 variables 에 없습니다.")
            continue
        va, vb = value_of(a), value_of(b)
        if va is None or vb is None:
            continue
        actual = "up" if va > vb else ("down" if va < vb else "flat")
        exp, verdict = d.get("expected"), c.get("verdict")
        if verdict == "supported" and actual != exp:
            out.append(f"checks[{i}]: 판정은 supported 인데 {d['from']}→{d['to']} 실제 방향은 {actual}(예상 {exp}) — "
                       "방향이 반대면 contradicted, 변화가 없으면 partially_supported 이하입니다.")
        elif verdict == "contradicted" and actual == exp:
            out.append(f"checks[{i}]: 판정은 contradicted 인데 {d['from']}→{d['to']} 실제 방향이 예상({exp})과 같습니다.")
    return out


def _all(text: str, run: dict) -> tuple[list[str], list[str], dict, set[str]]:
    """(오류, 경고, 변수, 틀린 변수 id)."""
    data, _, _, err = checks.locate_json(text)
    if err or not isinstance(data, dict) or "variables" not in data:
        return [], [], {}, set()
    vars_ = _variables(data)
    errs, warns, bad = [], [], set()
    for m in _PH.finditer(text):
        for vid in (m.group(2), m.group(3)):
            if vid and vid not in vars_:
                errs.append(f"자리표시자 {m.group(0)} 의 변수 {vid} 가 variables 에 없습니다.")
    # 깨진 자리표시자 — 정상 형식이 아닌데 변수처럼 생긴 것(예: '{chg:A,B)}'). 재요청은 JSON 안의 것만(2026-09-29):
    # 재요청은 JSON 만 다시 받으므로 글(A·B 절, JSON 뒤 메모)의 것은 고쳐지지 않아 재요청이 헛돌았다(PFS·AEE 각 2번).
    # 글의 것은 apply() 가 엄격한 조건으로 고쳐 채운다.
    _, js, je, _ = checks.locate_json(text)
    json_part = text[js:je] if js is not None else ""
    for m in _BROKEN_PH.finditer(_PH.sub("", json_part)):
        errs.append(f"자리표시자 형식이 틀렸습니다: {m.group(0)} — {{변수}} 또는 {{chg:나중,이전}} 형식으로(괄호는 중괄호만) 고치세요.")
    for path, s in _text_fields(data):
        naked = _BARE.findall(_PH.sub("", s))
        if naked:
            errs.append(f"{path}: 숫자를 직접 썼습니다({', '.join(naked[:3])}) — 숫자 자리에는 {{변수}}·{{chg:나중,이전}} 만 씁니다.")
    ev_by_id = {e.get("evidence_id"): e for e in data.get("evidence") or [] if isinstance(e, dict)}
    chunks = _load_chunks(run)
    for vid, v in vars_.items():
        e_, w_ = check_variable(v, ev_by_id, chunks)
        errs += e_
        warns += w_
        if e_:
            bad.add(vid)
    for m in _PH.finditer(text):
        if m.group(1) and m.group(3):
            a, b = vars_.get(m.group(2)), vars_.get(m.group(3))
            if a and b and (a.get("unit") != b.get("unit")):
                errs.append(f"{m.group(0)}: 두 변수의 단위가 다릅니다({a.get('unit')} / {b.get('unit')}).")
    errs += _verdict_problems(data, {k: v for k, v in vars_.items() if k not in bad})
    return errs, warns, vars_, bad


def problems(text: str, run: dict) -> list[str]:
    errs, _, _, _ = _all(text, run)
    return [f"{TAG} {e}" for e in errs]


# ---------------------------------------------------------------- 채우기

def korean_money(x: float) -> str:
    neg, x = x < 0, abs(x)
    if x >= 1e12:
        jo, eok = int(x // 1e12), round((x % 1e12) / 1e8)
        s = f"{jo:,}조 {eok:,}억 달러" if eok else f"{jo:,}조 달러"
    elif x >= 1e8:
        eok, man = int(x // 1e8), round((x % 1e8) / 1e4)
        if man == 10000:
            eok, man = eok + 1, 0
        s = f"{eok:,}억 {man:,}만 달러" if man else f"{eok:,}억 달러"
    elif x >= 1e4:
        s = f"{round(x / 1e4):,}만 달러"
    else:
        s = f"{x:,.0f}달러"
    return ("-" if neg else "") + s


def _dec(raw: str) -> int:
    s = _digits(str(raw))
    return len(s.split(".")[1]) if "." in s else 0


def display(v: dict, val: float) -> str:
    u = v.get("unit")
    if u == "USD":
        return korean_money(val)
    d = _dec(v.get("raw", ""))
    num = f"{val:,.{d}f}"
    return {"percent": f"{num}%", "bp": f"{num}bp", "ratio": f"{num}배", "days": f"{num}일"}.get(u, num)


def change(a: dict, va: float, b: dict, vb: float) -> str:
    diff = va - vb
    if diff == 0:
        return "변동 없음"
    u = a.get("unit")
    if u == "percent":
        d = max(_dec(a.get("raw", "")), _dec(b.get("raw", "")), 1)
        return f"{abs(diff):.{d}f}%p {'상승' if diff > 0 else '하락'}"
    if u == "bp":
        return f"{abs(diff):,.0f}bp {'상승' if diff > 0 else '하락'}"
    word = "증가" if diff > 0 else "감소"
    amount = korean_money(abs(diff)) if u == "USD" else f"{abs(diff):,.{max(_dec(a.get('raw', '')), 0)}f}"
    pct = f"({abs(diff) / abs(vb) * 100:.1f}%)" if vb else ""
    return f"{amount}{pct} {word}"


def apply(text: str, run: dict) -> tuple[str, list[str]]:
    """자리표시자를 채운다. 틀린 변수(재요청 뒤에도 남은 것)는 〔확인 안 됨〕. (새 글, 경고)."""
    errs, warns, vars_, bad = _all(text, run)
    if not vars_:
        return text, []
    vals = {}
    for vid, v in vars_.items():
        if vid in bad:
            continue
        try:
            val = value_of(v)
        except Exception:
            log.exception("변수 값 계산 실패 %s", vid)
            val = None
        if val is not None:
            vals[vid] = val

    def sub(m: re.Match) -> str:
        a = m.group(2)
        if m.group(1):
            b = m.group(3)
            if a in vals and b in vals:
                return change(vars_[a], vals[a], vars_[b], vals[b])
            return UNVERIFIED
        return display(vars_[a], vals[a]) if a in vals else UNVERIFIED

    # JSON 의 variables 에 확정 값·표시를 적어 다음 부서가 숫자를 쓸 수 있게 한다
    data, start, end, err = checks.locate_json(text)
    if not err and isinstance(data, dict):
        for v in data.get("variables") or []:
            if isinstance(v, dict) and v.get("id") in vars_:
                vid = v["id"]
                v["verified"] = vid in vals
                v["value"] = vals.get(vid)
                v["display"] = display(v, vals[vid]) if vid in vals else UNVERIFIED
        import json as _json
        text = checks.replace_json(text, _json.dumps(data, ensure_ascii=False, indent=2))
    text, repaired, dropped = repair_broken(text, set(vars_))
    total = len(_PH.findall(text)) + len(dropped)
    unfilled = 0

    def sub_count(m: re.Match) -> str:
        nonlocal unfilled
        s = sub(m)
        if s == UNVERIFIED:
            unfilled += 1
        return s

    out = _PH.sub(sub_count, text)
    notes = [f"[숫자 확인] {w}" for w in warns]
    if bad:
        notes.append(f"[숫자 확인] 확인하지 못한 변수 {len(bad)}개({', '.join(sorted(bad))})는 {UNVERIFIED}으로 채웠습니다.")
    if repaired:
        notes.append(f"[숫자 확인] 형식이 깨진 자리표시자 {len(repaired)}개를 서버가 고쳐 채웠습니다: {', '.join(repaired[:5])}")
    if dropped:
        notes.append(f"[숫자 확인] 고칠 수 없는 자리표시자 {len(dropped)}개는 {UNVERIFIED}으로 바꿨습니다: {', '.join(dropped[:5])}")
    notes.append(f"[숫자 채우기] 자리표시자 {total}개 · 채움 {total - unfilled - len(dropped)} · 안 채움 {unfilled + len(dropped)}"
                 f"(깨짐 {len(dropped)} · 확인 안 됨 {unfilled}) · 서버가 고친 깨진 표기 {len(repaired)}")
    return out, notes


# 깨진 자리표시자 고치기(2026-09-29) — 모델(deepseek-flash)이 두 변수 표기를 약 5% 확률로 '{chg:A,B)}' 처럼 함수 호출처럼
# 닫는다(9/28 실행 실측: 값 하나 {ID} 343개 중 0, {chg:A,B} 약 100개 중 5). 모양이 정확히 이 경우일 때만 고친다:
# 변수 이름 뒤에 괄호류·공백만 붙은 것 + 이름이 모두 variables 에 있을 때. 그 밖은 추측하지 않고 〔확인 안 됨〕.
_REPAIR = re.compile(r"\{(chg\s*:\s*)?([A-Z][A-Z0-9_]{1,40})(?:\s*,\s*([A-Z][A-Z0-9_]{1,40}))?[\s\)\]]*\}")


def repair_broken(text: str, known: set[str]) -> tuple[str, list[str], list[str]]:
    """(고친 글, 고친 표기 목록, 〔확인 안 됨〕으로 바꾼 표기 목록). 정상 자리표시자는 건드리지 않는다."""
    repaired: list[str] = []

    def fix(m: re.Match) -> str:
        a, b = m.group(2), m.group(3)
        canon = f"{{chg:{a},{b}}}" if m.group(1) and b else (f"{{{a}}}" if not m.group(1) and not b else None)
        if canon is None or m.group(0) == canon:
            return m.group(0)
        if a in known and (b is None or b in known):
            repaired.append(m.group(0))
            return canon
        return m.group(0)

    text = _REPAIR.sub(fix, text)
    dropped: list[str] = []
    rest = _PH.sub(lambda m: "\0" * len(m.group(0)), text)   # 정상 자리표시자 자리는 가려 두고 남은 깨진 것만 찾는다
    for m in reversed(list(_BROKEN_PH.finditer(rest))):
        dropped.append(text[m.start():m.end()])
        text = text[:m.start()] + UNVERIFIED + text[m.end():]
    return text, repaired, list(reversed(dropped))
