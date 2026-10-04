"""연구팀 공통 표준(표준 v1.2) — 공통 규칙 주입·공통 검증·기준값을 한 곳에서 다룬다(2026-09-26, 첫 실제 실행 반영).

원본 파일(서버 재시작 없이 매 실행 새로 읽는다):
  data/config/standard.json   기준값·적용 대상(applies_to)·공통 목록(금지 구절·앵커 허용 출처)·확인 도구·자료 주기(staleness_cadence)
  data/config/common_rules.md 공통 하드 제약 문구. {{version}}·{{thresholds.이름}} 은 standard.json 값으로 채운다

쓰는 곳:
  - app/chat/llm.py    : 적용 대상 직원의 업무 실행 시스템 프롬프트에 공통 규칙을 붙인다(rules_text)
  - app/runs/verify.py : 스키마 검사 뒤, 직원 훅보다 먼저 공통 검증(check)을 돌리고, 훅에 기준값(hook_config)을 넘긴다
  - app/runs/service.py: 실행 기록에 표준 버전(version_info)을 남기고, 글 부분의 검색 결과 번호를 지운다(strip_prose_footnotes)
  - app/tools/runner.py: 앵커 검색(anchor_only)의 허용 도메인(anchor_domains)

직원 훅은 격리 실행(python -I)이라 이 모듈을 import 할 수 없다 — 그래서 공통 검증은 여기서 하고, 훅에는 값만 넘긴다.

v1.2 개정(2026-09-26, 첫 실제 실행에서 발견한 문제 반영):
  - current_state.source_date 강제 거부 폐지 → 자료 주기별 허용 지연을 넘으면 [stale] 경고만(거부 안 함). 4개 훅의 중복 규칙 0을
    이곳(staleness_check)으로 옮겼다. 관측 종료일이 항상 '오늘'이라 예전 규칙은 거의 매 실행 재요청을 강제했다.
  - 통신사(로이터·AP·WSJ·FT 등)가 page_fetch 를 401/403 으로 막는 게 실제 실행에서 확인됨 → anchor_sources 재설계:
    통신사는 도메인만으로 앵커가 못 되고, 1차 원문(그 기사가 인용한 관보·기관 발표)이 앵커가 된다. 1차 원문이 없을 때만
    통신사를 '최후 수단 앵커'로 쓰되 date_source=search_metadata(미검증)를 붙이고 강도·확신도 상한이 걸린다(anchor_kind).
"""

import datetime as _dt
import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from app.config import DATA_DIR
from app.runs import checks

log = logging.getLogger("agent_town.runs")

CONFIG_PATH = DATA_DIR / "config" / "standard.json"
RULES_PATH = DATA_DIR / "config" / "common_rules.md"
FRED_FREQUENCY_CACHE_PATH = DATA_DIR / "config" / "fred_frequency_cache.json"

_FOOTNOTE = re.compile(r"\[W\d+\]")

# ---------------------------------------------------------------- 오류 분류 코드와 재시도 정책(표준 v1.1 §5-3, 2026-09-26 결정)
# format·calc_error·citation_error·policy_violation → JSON 만 다시 요청 / data_missing → 재요청 없이 '보류'로 통과(다음 단계에 표시)
# tool_failure → 도구 호출을 2회 더 시도(runner), 검사에서 남으면 실행 실패 / legacy_unclassified → 재요청 없이 멈추고 기록
RETRY_CODES = {"format", "calc_error", "citation_error", "policy_violation"}
HOLD_CODES = {"data_missing"}
FAIL_CODES = {"tool_failure"}
STOP_CODES = {"legacy_unclassified"}
_CODE = re.compile(r"^\[(format|calc_error|citation_error|data_missing|tool_failure|policy_violation|legacy_unclassified)\]\s*")


def code_of(problem: str) -> str:
    """오류 문장 앞의 [코드]. 코드가 없으면 legacy_unclassified(분류 안 됨 → 재요청 없이 멈춤)."""
    m = _CODE.match(str(problem or ""))
    return m.group(1) if m else "legacy_unclassified"


def with_code(problem: str, code: str) -> str:
    """코드가 없는 문장에 코드를 붙인다(이미 있으면 그대로)."""
    return problem if _CODE.match(str(problem or "")) else f"[{code}] {problem}"


def classify(problem: str, cfg: dict | None = None) -> str:
    """직원 훅 오류에 분류 코드를 붙인다 — 분류표는 standard.json 의 error_codes 하나(훅마다 복사하지 않음).
    위에서부터 처음 맞는 문구의 코드, 맞는 것이 없으면 legacy_unclassified(재요청 없이 멈추고 기록)."""
    if _CODE.match(str(problem or "")):
        return problem
    for needle, code in ((load() if cfg is None else cfg).get("error_codes") or []):
        if needle in str(problem):
            return f"[{code}] {problem}"
    return f"[legacy_unclassified] {problem}"


def platform_code(problem: str) -> str:
    """플랫폼이 만드는 오류(필수 도구·전문자료·포트폴리오 검사)의 코드."""
    p = str(problem or "")
    if "호출이 모두 실패" in p:
        return "tool_failure"
    if "전문자료" in p and "얻지 못" in p:
        return "data_missing"
    if "포트폴리오" in p or "pm_build_portfolio" in p:
        return "calc_error"
    return "legacy_unclassified"
_FR_NO = re.compile(r"\b(20\d\d-\d{4,6})\b")
_FRED = re.compile(r"fred\.stlouisfed\.org/(?:series/|graph/\?id=)([A-Za-z0-9_]+)")


# ---------------------------------------------------------------- 설정

def load() -> dict:
    """standard.json. 없거나 깨지면 {} — 그때는 적용 대상이 없어 공통 규칙·검증이 꺼진다(기존 동작 그대로)."""
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError:
        log.exception("표준 설정 파일이 깨졌습니다: %s", CONFIG_PATH)
        return {}


def is_member(agent_id: str | None, cfg: dict | None = None) -> bool:
    cfg = load() if cfg is None else cfg
    return bool(agent_id) and agent_id in (cfg.get("applies_to") or {})


def thresholds(cfg: dict | None = None) -> dict:
    """{"이름": 값} — 설정의 {"value": …, "provisional": …} 꼴을 값만으로 편다."""
    cfg = load() if cfg is None else cfg
    out = {}
    for k, v in (cfg.get("thresholds") or {}).items():
        out[k] = v.get("value") if isinstance(v, dict) else v
    return out


def version_info(cfg: dict | None = None) -> dict:
    """실행 기록용: 표준 버전과 설정·공통 규칙 파일의 지문(같은 버전 이름으로 파일만 고친 경우도 구별되게)."""
    cfg = load() if cfg is None else cfg
    h = hashlib.sha256()
    for p in (CONFIG_PATH, RULES_PATH):
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"-")
    return {"version": cfg.get("version") or "-", "fingerprint": h.hexdigest()[:12]}


def hook_config(cfg: dict | None = None) -> dict:
    """직원 훅에 넘기는 값(훅 모듈의 PLATFORM_CONFIG). 훅은 숫자를 직접 적지 않고 여기서 읽는다."""
    cfg = load() if cfg is None else cfg
    return {"version": cfg.get("version"), "thresholds": thresholds(cfg), "anchor_sources": cfg.get("anchor_sources") or {},
            "forbidden_phrases": cfg.get("forbidden_phrases") or {}}   # 표준 적용 대상이 아닌 직원(⑦ 등)의 훅도 같은 구절 목록을 쓰게


def anchor_domains(cfg: dict | None = None) -> list[str]:
    """anchor_only 검색(넓게 찾을 도메인) — 1차 자료 + 통신사(단서로도 찾아야 하므로 검색 대상에는 포함).
    앵커 '자격'(_anchor_kind)과는 다르다: 검색 대상 ≠ 앵커로 인정되는 출처."""
    a = ((load() if cfg is None else cfg).get("anchor_sources") or {})
    out: list[str] = []
    for k in ("primary_domains", "major_wire_domains"):
        for d in a.get(k) or []:
            d = str(d).strip().lower()
            if d and d not in out:
                out.append(d)
    return out


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def anchor_kind(evidence: dict, cfg: dict | None = None) -> str:
    """이 근거가 앵커 자격으로 어디에 해당하는가(표준 v1.2). 훅의 _allowlisted_anchor 대신 여기서 판정한다(기준 하나).
      "primary"       — 1차 자료 도메인(관보·통계·기관). 정상 앵커, 발행일 확인(page_fetch/1차 도구) 요구.
      "wire_last_resort" — 통신사(그 자체)를 최후 수단 앵커로 쓴 경우. date_source=search_metadata 여야 하고
                         강도·확신도 상한(thresholds.max_conf_unverified_anchor 등)이 걸린다.
      "none"          — 앵커 자격 없음(허용 목록 밖의 출처). is_anchor=true 면 거부."""
    a = ((load() if cfg is None else cfg).get("anchor_sources") or {})
    host = _host(evidence.get("url", ""))
    src = (evidence.get("source") or "").lower()
    primary = [str(d).lower() for d in a.get("primary_domains") or []]
    if host and any(host == d or host.endswith("." + d) for d in primary):
        return "primary"
    if any(n in src for n in primary):
        return "primary"
    wires = [str(d).lower() for d in a.get("major_wire_domains") or []] + [str(n).lower() for n in a.get("major_wire_names") or []]
    if (host and any(host == d or host.endswith("." + d) for d in a.get("major_wire_domains") or [])) or any(n in src for n in a.get("major_wire_names") or []):
        return "wire_last_resort"
    return "none"


# ---------------------------------------------------------------- 자료 주기(staleness, 표준 v1.2)

def _parse_date(s) -> _dt.date | None:
    try:
        return _dt.date.fromisoformat(str(s).strip()[:10])
    except (ValueError, TypeError):
        return None


def _business_days_between(d1: _dt.date, d2: _dt.date) -> int:
    """d1~d2 사이 영업일 수(월~금만, 공휴일은 안 뺀다 — 간단한 근사)."""
    if d1 > d2:
        d1, d2 = d2, d1
    days, d = 0, d1
    while d < d2:
        d += _dt.timedelta(days=1)
        days += d.weekday() < 5
    return days


def _load_fred_cache() -> dict:
    try:
        return json.loads(FRED_FREQUENCY_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_fred_cache(cache: dict) -> None:
    try:
        FRED_FREQUENCY_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        log.warning("FRED 주기 캐시 저장 실패: %s", FRED_FREQUENCY_CACHE_PATH)


def fred_frequency_short(series_id: str) -> str | None:
    """FRED 시리즈의 frequency_short(D/W/BW/M/Q/SA/A 등, fred/series 응답)를 조회한다.
    로컬 캐시(data/config/fred_frequency_cache.json)에 남겨 같은 시리즈를 다시 조회하지 않는다.
    키가 없거나 조회에 실패하면 None(호출자가 월간 기본값으로 처리)."""
    cache = _load_fred_cache()
    if series_id in cache:
        return cache[series_id].get("frequency_short")
    key = os.getenv("FRED_API_KEY")
    if not key:
        return None
    url = ("https://api.stlouisfed.org/fred/series?series_id=" + urllib.parse.quote(series_id) +
           "&api_key=" + urllib.parse.quote(key) + "&file_type=json")
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        log.warning("FRED frequency 조회 실패(series_id=%s): %s", series_id, exc)
        return None
    freq = ((body.get("seriess") or [{}])[0]).get("frequency_short")
    cache[series_id] = {"frequency_short": freq, "fetched_at": _dt.date.today().isoformat()}
    _save_fred_cache(cache)
    return freq


def _evidence_cadence_bucket(ev: dict, cfg: dict) -> str | None:
    """근거 하나의 자료 주기 버킷. None 은 '신선도 검사 대상이 아님'(관보 등 시계열이 아닌 문서)."""
    host = _host(str(ev.get("url") or ""))
    excluded = [str(d).lower() for d in cfg.get("staleness_excluded_domains") or []]
    if host and any(host == d or host.endswith("." + d) for d in excluded):
        return None
    sid = ev.get("series_id")
    if sid:
        freq = fred_frequency_short(str(sid))
        bucket = (cfg.get("fred_frequency_map") or {}).get(freq or "")
        if bucket:
            return bucket
    census = [str(d).lower() for d in cfg.get("staleness_census_domains") or []]
    if host and any(host == d or host.endswith("." + d) for d in census):
        return "monthly"
    return "monthly"          # 주기를 못 정한 자료 — 월간(45일)로 처리(표준 v1.2.1)


_CADENCE_ORDER = {"daily": 0, "weekly": 1, "monthly": 2, "quarterly": 3}


def signal_cadence_bucket(signal: dict, evidence: list[dict], cfg: dict | None = None) -> str | None:
    """신호가 인용한 근거(evidence_ids) 중 시계열 근거의 가장 짧은(가장 잦은) 주기를 쓴다.
    인용 근거를 찾았는데 전부 신선도 검사 제외 대상이면 None(그 신호는 신선도 검사를 건너뛴다).
    인용 근거를 하나도 못 찾았으면 월간 기본값."""
    cfg = load() if cfg is None else cfg
    ev_map = {e.get("evidence_id"): e for e in evidence if isinstance(e, dict)}
    buckets, saw_evidence = [], False
    for eid in signal.get("evidence_ids") or []:
        ev = ev_map.get(eid)
        if ev is None:
            continue
        saw_evidence = True
        b = _evidence_cadence_bucket(ev, cfg)
        if b is not None:
            buckets.append(b)
    if saw_evidence and not buckets:
        return None
    if not buckets:
        return "monthly"
    return min(buckets, key=lambda b: _CADENCE_ORDER.get(b, 2))


def staleness_check(data: dict, cfg: dict | None = None) -> tuple[list[str], list[str]]:
    """current_state 검사(표준 v1.2) — 기자 4개 훅의 중복 규칙 0을 이곳으로 옮겼다.
    still_valid=false 인데 신호로 냈으면 오류. source_date 는 더는 강제로 거부하지 않고,
    신호가 인용한 근거의 자료 주기(표준 v1.2.1: FRED frequency_short·Census 월간 고정·관보 등 제외)별
    허용 지연(설정값)을 넘으면 [stale] 경고만 남긴다(재요청을 유발하지 않는다)."""
    cfg = load() if cfg is None else cfg
    window = data.get("window") or {}
    window_end = window.get("end", "")
    we = _parse_date(window_end)
    evidence = data.get("evidence") or []
    errs, warns = [], []
    for s in data.get("signals") or []:
        if not isinstance(s, dict):
            continue
        cs = s.get("current_state") or {}
        sid = s.get("signal_id", "?")
        if not cs.get("still_valid", False):
            errs.append(f"[policy_violation] {sid}: current_state.still_valid 가 false 인데 신호로 냈습니다 — 이미 뒤집혔거나 철회됐거나 무효 조건이 걸렸으면 신호를 내지 말고 notes 에 기록하세요.")
        bucket = signal_cadence_bucket(s, evidence, cfg)
        if bucket is None:
            continue           # 인용 근거가 전부 시계열이 아님(관보 등) — 신선도 검사 대상 아님
        sd = _parse_date(cs.get("source_date", ""))
        if we and sd and sd < we:
            cadence = (cfg.get("staleness_cadence") or {}).get(bucket) or {"unit": "days", "tolerance": 10}
            unit_kr = "영업일" if cadence.get("unit") == "business_days" else "일"
            lag = _business_days_between(sd, we) if cadence.get("unit") == "business_days" else (we - sd).days
            if lag > cadence.get("tolerance", 10):
                warns.append(f"[stale] {sid}: current_state.source_date({cs.get('source_date')})가 관측 종료일({window_end})보다 {lag}{unit_kr} 이전입니다 — "
                            f"{bucket} 주기 허용 지연({cadence.get('tolerance')}{unit_kr})을 넘었습니다(오래된 자료로 표시만 합니다, 재요청 아님).")
    return errs, warns


def render_text(text: str, cfg: dict | None = None) -> str:
    """글 속의 {{version}}·{{thresholds.이름}} 을 설정값으로 채운다(지시문·수행 지시·공통 규칙에 숫자를 복사하지 않기 위해).
    모르는 자리표시(업무 입력 {{window_start}} 등)는 그대로 둔다."""
    if not text or "{{" not in text:
        return text
    cfg = load() if cfg is None else cfg
    values = {"version": str(cfg.get("version") or "")} | {f"thresholds.{k}": str(v) for k, v in thresholds(cfg).items()}
    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", lambda m: values.get(m.group(1), m.group(0)), text)


def rules_text(cfg: dict | None = None) -> str:
    """공통 규칙 문구(자리표시를 설정값으로 채운 것)."""
    cfg = load() if cfg is None else cfg
    try:
        text = RULES_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return render_text(text, cfg)


def apply_schema_thresholds(schema, cfg: dict | None = None):
    """스키마에서 "x-std-threshold": {"maximum": "이름", …} 표시가 있는 자리의 값을 설정값으로 채운 사본을 돌려준다.
    표시 키는 지운다(모델에게 보여 주는 스키마를 깔끔하게). 표시가 없으면 원본 그대로(복사 비용도 없다)."""
    if not isinstance(schema, dict) or "x-std-threshold" not in json.dumps(schema):
        return schema
    th = thresholds(load() if cfg is None else cfg)

    def walk(o):
        if isinstance(o, dict):
            out = {k: walk(v) for k, v in o.items() if k != "x-std-threshold"}
            for key, name in (o.get("x-std-threshold") or {}).items():
                if name in th:
                    out[key] = th[name]
            return out
        if isinstance(o, list):
            return [walk(v) for v in o]
        return o
    return walk(schema)


# ---------------------------------------------------------------- 실행 기록 대조(확인 여부)

def _norm(url) -> str:
    try:
        p = urllib.parse.urlsplit(str(url or "").strip())
    except ValueError:
        return ""
    host = (p.hostname or "").lower().removeprefix("www.")
    return (host + p.path.rstrip("/") + (("?" + p.query) if p.query else "")) if host else ""


def _url_keys(url) -> set[str]:
    u = str(url or "")
    keys = {_norm(u)}
    if "federalregister" in u:
        keys |= {"fr:" + m for m in _FR_NO.findall(u)}
    m = _FRED.search(u)
    if m:
        keys.add("fred:" + m.group(1).upper())
    return {k for k in keys if k}


def confirmed_keys(sources: list[dict], cfg: dict | None = None) -> set[str]:
    """이번 실행에서 발행일이 확인된 자료의 열쇠(주소·관보 문서번호·FRED 시리즈) 모음."""
    cfg = load() if cfg is None else cfg
    tools = cfg.get("date_confirming_tools") or {}
    any_ok, dated_ok = set(tools.get("any") or []), set(tools.get("dated") or [])
    keys: set[str] = set()
    for s in sources or []:
        if s.get("via") != "tool" or not s.get("ok"):
            continue
        t, args, items = s.get("tool"), s.get("args") or {}, s.get("items") or []
        if t not in any_ok | dated_ok:
            continue
        if t in ("fred_series", "fred_release_dates") and args.get("series_id"):
            keys.add("fred:" + str(args["series_id"]).upper())
        dated = [it for it in items if it.get("date")]
        for it in (items if t in any_ok else dated):
            keys |= _url_keys(it.get("url"))
        if t == "page_fetch" and dated:          # 요청한 주소도(재지정으로 최종 주소가 달라진 경우)
            keys |= _url_keys(args.get("url"))
    return keys


def _evidence_keys(e: dict) -> set[str]:
    keys = _url_keys(e.get("url"))
    if e.get("series_id"):
        keys.add("fred:" + str(e["series_id"]).upper())
    doc = str(e.get("doc_id") or "")
    if doc.upper().startswith("FR"):
        keys |= {"fr:" + m for m in _FR_NO.findall(doc)}
    return keys


# ---------------------------------------------------------------- 공통 검증

def _strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)


def _phrase_hits(text: str, fp: dict) -> list[str]:
    low = text.lower()
    for ok in fp.get("allowed_event_phrases") or []:   # 사건 이름("임원 매수")은 먼저 지운다
        low = low.replace(ok.lower(), " ")
    return sorted({p for p in fp.get("phrases") or [] if p.lower() in low})


def check(data, text: str, sources: list[dict] | None, agent_id: str | None) -> tuple[list[str], list[str]]:
    """(오류, 경고). 적용 대상이 아니면 둘 다 빈 목록. 오류 문장 앞의 [코드]는 표준 v1.1 §5-3 분류다.
    sources 가 None 이면(검증 시험 화면 등 실행 기록이 없는 경우) 실행 기록 대조는 건너뛴다."""
    cfg = load()
    if not is_member(agent_id, cfg) or not isinstance(data, dict):
        return [], []
    errs, warns = [], []
    evidence = [e for e in (data.get("evidence") or []) if isinstance(e, dict)]
    date_fields = cfg.get("date_fields") or ["released_at_et"]

    # 0) 현재 상태(still_valid) + 자료 주기별 발행일 지연(표준 v1.2, 훅 4개의 중복 규칙 0을 이곳으로 옮김)
    e0, w0 = staleness_check(data, cfg)
    errs += e0
    warns += w0

    # 1) 날짜 일관성 — 공개일과 확인 여부는 함께 간다(추측 날짜 방지). 예전에는 기자 훅 4개에 같은 코드가 있었다.
    # 표준 v1.2 예외: date_source=search_metadata(통신사 최후 수단 앵커, 아래 5번)는 공개일이 있어도 date_confirmed=false 가 정상이다.
    for e in evidence:
        if "date_confirmed" not in e:
            continue
        eid = e.get("evidence_id", "?")
        field = next((f for f in date_fields if f in e), date_fields[0])
        has_date = bool(str(e.get(field) or "").strip())
        confirmed = e.get("date_confirmed") is True
        fallback = e.get("date_source") == "search_metadata"
        if has_date and not confirmed and not fallback:
            errs.append(f"[citation_error] {eid}: {field} 이 있는데 date_confirmed=false 입니다 — 발행일을 확인하지 못했다면 "
                        f"{field} 을 null 로 두세요(검색 결과 날짜·오늘 날짜 추측 금지). 확인했다면 date_confirmed=true 로 하세요.")
        if confirmed and not has_date:
            errs.append(f"[citation_error] {eid}: date_confirmed=true 인데 {field} 이 비어 있습니다 — 확인한 발행일을 적으세요.")
        if fallback and not has_date:
            errs.append(f"[citation_error] {eid}: date_source=search_metadata 인데 {field} 이 비어 있습니다 — 검색 메타데이터의 발행일을 적으세요.")

    # 2) 확신도 상한 — 발행일이 확인된 근거가 하나도 없는 신호(예전에는 기자 훅 4개에 0.4 로 박혀 있었다)
    cap = thresholds(cfg).get("max_conf_unconfirmed")
    by_id = {e.get("evidence_id"): e for e in evidence}
    if cap is not None:
        for s in data.get("signals") or []:
            if not isinstance(s, dict):
                continue
            refs = [by_id[r] for r in s.get("evidence_ids") or [] if r in by_id]
            conf = s.get("confidence")
            if refs and not any(e.get("date_confirmed") is True for e in refs) and isinstance(conf, (int, float)) \
                    and not isinstance(conf, bool) and conf > cap:
                errs.append(f"[policy_violation] {s.get('signal_id', '?')}: 발행일이 확인된 근거(date_confirmed=true)가 하나도 없는데 "
                            f"confidence={conf} — {cap} 이하여야 합니다(발행일을 확인하거나 확신도를 낮추세요).")

    # 2-b) 앵커 등급(표준 v1.2, 2026-09-26) — primary(1차 자료)만 정상 앵커. 통신사 등은 도메인만으로 앵커가 못 된다;
    # 1차 원문을 못 찾아 통신사를 최후 수단으로 쓸 때는 date_source=search_metadata 표시가 있어야 하고, 그 앵커에
    # 기대는 신호는 강도·확신도 상한이 걸린다(로이터·AP·WSJ·FT 의 page_fetch 차단이 실제 실행에서 확인된 뒤 도입).
    if any("is_anchor" in e for e in evidence):
        mag_cap = thresholds(cfg).get("max_magnitude_unverified_anchor")
        conf_cap2 = thresholds(cfg).get("max_conf_unverified_anchor")
        kind_by_id: dict[str, str] = {}
        for e in evidence:
            if not e.get("is_anchor"):
                continue
            eid = e.get("evidence_id", "?")
            kind = anchor_kind(e, cfg)
            kind_by_id[eid] = kind
            if kind == "none":
                errs.append(f"[citation_error] {eid}: is_anchor=true 인데 허용 앵커 출처가 아닙니다({e.get('source', '')}) — "
                            "앵커는 1차 자료(관보·정부기관·통계)이거나, 1차 원문을 못 찾았을 때만 통신사를 최후 수단으로 쓸 수 있습니다"
                            "(그때는 date_source=search_metadata 로 표시하세요).")
            elif kind == "wire_last_resort" and e.get("date_source") != "search_metadata":
                errs.append(f"[citation_error] {eid}: 통신사 근거를 앵커로 쓰려면 먼저 그 기사가 인용한 1차 원문(관보·USTR·상무부·OFAC 등)을 찾아 앵커로 삼으세요. "
                            "정말 1차 원문이 없다면 date_source=search_metadata 로 표시하고 검색 메타데이터의 발행일을 적으세요(강도·확신도 상한이 걸립니다).")
        if kind_by_id:
            for s in data.get("signals") or []:
                if not isinstance(s, dict):
                    continue
                sid = s.get("signal_id", "?")
                refs = [by_id[r] for r in s.get("evidence_ids") or [] if r in by_id]
                if not any(kind_by_id.get(e.get("evidence_id")) == "wire_last_resort" for e in refs if e.get("is_anchor")):
                    continue
                mag, conf = s.get("magnitude"), s.get("confidence")
                if mag_cap is not None and isinstance(mag, (int, float)) and not isinstance(mag, bool) and mag > mag_cap:
                    errs.append(f"[policy_violation] {sid}: 앵커의 발행일을 검색 메타데이터로만 확인했는데(date_source=search_metadata) magnitude={mag} — {mag_cap} 이하여야 합니다.")
                if conf_cap2 is not None and isinstance(conf, (int, float)) and not isinstance(conf, bool) and conf > conf_cap2:
                    errs.append(f"[policy_violation] {sid}: 앵커의 발행일을 검색 메타데이터로만 확인했는데(date_source=search_metadata) confidence={conf} — {conf_cap2} 이하여야 합니다.")

    # 3) 확인 여부를 실행 기록과 대조 — 앵커가 어긋나면 거부, 그 밖의 근거는 경고(2026-09-26 결정)
    if sources is not None:
        seen = confirmed_keys(sources, cfg)
        for e in evidence:
            if e.get("date_confirmed") is not True or (_evidence_keys(e) & seen):
                continue
            eid = e.get("evidence_id", "?")
            msg = (f"{eid}: date_confirmed=true 인데 이번 실행에서 이 근거({str(e.get('url') or e.get('doc_id') or '')[:90]})를 "
                   "1차 도구나 페이지 조회로 열어 발행일을 확인한 기록이 없습니다")
            if e.get("is_anchor") is True:
                errs.append(f"[citation_error] {msg} — 앵커 근거는 발행일을 실제로 확인해야 합니다. 확인하지 않았다면 date_confirmed=false·공개일 null 로 "
                            "두고, 앵커를 확인된 1차 자료로 바꾸거나 신호를 notes 로 내리세요.")
            else:
                warns.append(f"[citation_error] {msg} (보강 근거라 경고만).")

    # 4) 금지 표현·검색 결과 번호 — JSON(실제 산출물)은 오류(형식 재요청으로 고칠 수 있음), 글 부분은 경고
    fp = cfg.get("forbidden_phrases") or {}
    blob = "\n".join(_strings(data))
    hits = _phrase_hits(blob, fp)
    if hits:
        errs.append(f"[policy_violation] JSON 에 매매 권유 표현이 있습니다: {', '.join(hits)} — 사건 이름이 아니라면 지우세요.")
    notes = sorted(set(_FOOTNOTE.findall(blob)))
    if notes:
        errs.append(f"[policy_violation] JSON 에 검색 결과 번호가 남았습니다: {', '.join(notes[:8])} — 근거는 자체 식별자(D-#### 등)로 인용하세요.")
    prose = _prose(text)
    p_hits = _phrase_hits(prose, fp)
    if p_hits:
        warns.append(f"[policy_violation] 글 부분에 매매 권유 표현: {', '.join(p_hits)}")
    return errs, warns


def _prose(text: str) -> str:
    """JSON 블록을 뺀 글 부분."""
    _, start, end, err = checks.locate_json(text or "")
    if err or start is None:
        return text or ""
    return (text or "")[:start] + (text or "")[end:]


def strip_prose_footnotes(text: str) -> tuple[str, int]:
    """글 부분(JSON 밖)의 검색 결과 번호([W4] 등)를 지운다. JSON 안은 건드리지 않는다(거기는 검증에서 오류로 잡는다)."""
    _, start, end, err = checks.locate_json(text or "")
    if err or start is None:
        new, n = _FOOTNOTE.subn("", text or "")
        return new, n
    head, n1 = _FOOTNOTE.subn("", text[:start])
    tail, n2 = _FOOTNOTE.subn("", text[end:])
    return head + text[start:end] + tail, n1 + n2
