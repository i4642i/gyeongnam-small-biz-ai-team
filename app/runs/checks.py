"""업무 결과 검사: 모델이 낸 결과가 정해진 형식을 지켰는지 서버가 확인한다.

업무마다 `check`(JSON)를 정해 두면, 실행이 끝난 뒤 이 검사를 돌리고 통과하지 못하면 한 번 다시 시킨다(모델의 판단이 아니라 서버가 강제).
지원하는 검사(모두 선택):

  {
    "json": true,                                   # 답변 안의 ```json 블록(마지막 것)이 있고 JSON 으로 읽혀야 한다
    "schema": {...},                                # 그 JSON 의 형식(JSON Schema 의 일부: type properties required enum pattern minimum maximum items minItems minLength additionalProperties)
    "coverage": [{"values": ["A","B"], "groups": ["x[].f", "y[]"]}],   # values 의 각 값이 groups 중 정확히 한 곳에 나와야 한다
    "refs": [{"from": "signals[].evidence_ids[]", "to": "evidence[].evidence_id"}],   # from 의 값은 모두 to 안에 있어야 한다
    "when": [{"path": "signals[]", "if": {"magnitude": {"gte": 2}}, "require": ["market_reaction"]}]   # 조건을 만족하는 항목은 필드가 있어야 한다
  }

경로: 점(.)으로 이어 쓰고, 목록은 이름 뒤에 []를 붙인다(예: signals[].evidence_ids[]).
JSON Schema 전체를 구현한 것이 아니라 위 일부만 지원한다(별도 패키지 없이 동작).
"""

import json
import re

MAX_SPEC_CHARS = 30000
MAX_PROBLEMS = 20

_FENCE = re.compile(r"```[ \t]*([A-Za-z]*)[ \t]*\r?\n(.*?)```", re.S)
_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}
_SCHEMA_KEYS = {"type", "properties", "required", "enum", "pattern", "minimum", "maximum", "items", "minItems", "minLength", "additionalProperties",
                "description", "title", "default", "$schema", "examples"}
_OPS = {"gte": lambda a, b: a >= b, "gt": lambda a, b: a > b, "lte": lambda a, b: a <= b, "lt": lambda a, b: a < b, "eq": lambda a, b: a == b, "ne": lambda a, b: a != b}


class SpecError(ValueError):
    pass


# ---------------------------------------------------------------- 설정 검증(저장할 때)

def validate_spec(spec) -> dict:
    """저장 전에 검사 설정이 올바른지 확인한다. 잘못이면 SpecError(사람이 읽을 문장)."""
    if not isinstance(spec, dict):
        raise SpecError("출력 검사 설정은 JSON 객체여야 합니다.")
    if len(json.dumps(spec, ensure_ascii=False)) > MAX_SPEC_CHARS:
        raise SpecError(f"출력 검사 설정이 너무 깁니다(최대 {MAX_SPEC_CHARS}자).")
    unknown = set(spec) - {"json", "schema", "coverage", "refs", "when"}
    if unknown:
        raise SpecError(f"알 수 없는 검사 항목입니다: {', '.join(sorted(unknown))} (json, schema, coverage, refs, when 만 쓸 수 있습니다)")
    if ("schema" in spec or "coverage" in spec or "refs" in spec or "when" in spec) and spec.get("json") is not True:
        raise SpecError('schema·coverage·refs·when 은 결과 안의 JSON 을 검사하므로 "json": true 가 필요합니다.')
    if "json" in spec and not isinstance(spec["json"], bool):
        raise SpecError('"json" 은 true/false 여야 합니다.')
    if "schema" in spec:
        _check_schema(spec["schema"], "schema")
    for key, fields in (("coverage", ("values", "groups")), ("refs", ("from", "to")), ("when", ("path", "if", "require"))):
        items = spec.get(key, [])
        if not isinstance(items, list) or len(items) > 20:
            raise SpecError(f'"{key}" 는 20개 이하의 목록이어야 합니다.')
        for i, it in enumerate(items):
            if not isinstance(it, dict) or not all(f in it for f in fields) or set(it) - set(fields) - {"label"}:
                raise SpecError(f'"{key}"[{i}] 에는 {", ".join(fields)} 가 있어야 합니다.')
    for it in spec.get("coverage", []):
        if not (isinstance(it["values"], list) and it["values"] and isinstance(it["groups"], list) and len(it["groups"]) >= 2):
            raise SpecError('"coverage" 는 values(1개 이상)와 groups(2개 이상)가 필요합니다.')
        for p in it["groups"]:
            _check_path(p)
    for it in spec.get("refs", []):
        _check_path(it["from"]); _check_path(it["to"])
    for it in spec.get("when", []):
        _check_path(it["path"])
        cond = it["if"]
        if not isinstance(cond, dict) or not cond or not all(isinstance(v, dict) and set(v) <= set(_OPS) or not isinstance(v, dict) for v in cond.values()):
            raise SpecError('"when"의 "if" 는 {"필드": {"gte": 2}} 또는 {"필드": 값} 형태여야 합니다(gte gt lte lt eq ne).')
        if not (isinstance(it["require"], list) and it["require"] and all(isinstance(x, str) for x in it["require"])):
            raise SpecError('"when"의 "require" 는 필드 이름 목록이어야 합니다.')
    return spec


def _check_path(p) -> None:
    if not isinstance(p, str) or not re.fullmatch(r"[A-Za-z0-9_]+(\[\])?(\.[A-Za-z0-9_]+(\[\])?)*", p):
        raise SpecError(f"경로 형식이 올바르지 않습니다: {p!r} (예: signals[].evidence_ids[])")


def _check_schema(node, where: str) -> None:
    if not isinstance(node, dict):
        raise SpecError(f"{where}: 스키마는 객체여야 합니다.")
    bad = set(node) - _SCHEMA_KEYS
    if bad:
        raise SpecError(f"{where}: 지원하지 않는 스키마 키워드입니다: {', '.join(sorted(bad))}")
    t = node.get("type")
    for x in (t if isinstance(t, list) else [t] if t else []):
        if x not in _TYPES and x not in ("number", "integer"):
            raise SpecError(f"{where}: 알 수 없는 type 입니다: {x}")
    if "pattern" in node:
        try:
            re.compile(node["pattern"])
        except re.error:
            raise SpecError(f"{where}: pattern 이 올바른 정규식이 아닙니다.")
    for k, sub in (node.get("properties") or {}).items():
        _check_schema(sub, f"{where}.{k}")
    if "items" in node:
        _check_schema(node["items"], f"{where}[]")


# ---------------------------------------------------------------- 결과에서 JSON 꺼내기

def locate_json(text: str) -> tuple[object | None, int | None, int | None, str | None]:
    """(값, 시작, 끝, 오류): 답변 안의 JSON 을 찾는다. 시작·끝은 JSON 글이 text 안에서 차지하는 자리(바꿔 끼울 때 쓴다).

    순서: ① 응답 전체가 JSON 이면 그대로 → ② ```json 코드 블록(마지막 것) → ③ 코드 블록이 없으면 마지막 JSON 객체.
    응답이 `{`로 시작하는데 읽지 못하면 ③으로 넘어가지 않고 오류로 돌려준다(글 속의 작은 객체를 통째로 착각하지 않게).
    """
    stripped = text.strip()
    lead = len(text) - len(text.lstrip())
    whole_error = None
    if stripped.startswith(("{", "[")):
        try:
            return json.loads(stripped), lead, lead + len(stripped), None
        except json.JSONDecodeError as e:
            whole_error = f"JSON 을 읽지 못했습니다({e.msg}, {e.lineno}행 {e.colno}열)."
    cands = [m for m in _FENCE.finditer(text) if m.group(1).lower() == "json" or m.group(2).lstrip().startswith(("{", "["))]
    if cands:
        m = cands[-1]
        try:
            return json.loads(m.group(2)), m.start(2), m.end(2), None
        except json.JSONDecodeError as e:
            return None, m.start(2), m.end(2), f"JSON 블록을 읽지 못했습니다({e.msg}, {e.lineno}행 {e.colno}열)."
    if whole_error:
        return None, None, None, whole_error
    if text.count("```") % 2 == 1:
        # 여는 코드펜스(```)는 있는데 닫는 펜스가 없다 — 응답이 도중에 잘린 것이다(출력 토큰 한도 도달 등).
        # 이럴 때 아래(③)로 넘어가면 잘린 본문 뒤쪽에 우연히 남은 작은 JSON 조각(예: notes 안의 작은 객체)을
        # 전체 결과로 잘못 골라 쓸 수 있어서, 여기서 분명한 오류로 끊는다.
        return None, None, None, "출력이 도중에 잘린 것으로 보입니다(코드펜스 ```가 닫히지 않았습니다). 뒤쪽 JSON 조각을 대신 읽지 않았습니다 — 출력 토큰 한도를 늘리거나 출력을 줄여서 다시 받아야 합니다."
    decoder, i, last = json.JSONDecoder(), 0, None
    while True:
        i = text.find("{", i)
        if i == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, i)
            last, i = (obj, i, end), end
        except json.JSONDecodeError:
            i += 1
    if last is not None:
        return last[0], last[1], last[2], None
    return None, None, None, "답변에 JSON 객체가 없습니다."


def extract_json(text: str) -> tuple[object | None, str | None, str | None]:
    """(값, 원문, 오류). locate_json 의 간단한 형태."""
    data, start, end, err = locate_json(text)
    return data, (text[start:end] if start is not None else None), err


def replace_json(text: str, new_json: str) -> str:
    """text 안의 JSON 자리(locate_json 이 찾은 곳)를 new_json 으로 바꾼다. JSON 이 없으면 끝에 ```json 블록으로 붙인다.
    코드 블록 안이면 앞뒤 줄바꿈을 지키고, 응답 전체가 JSON 이었으면 그 자리만 바꾼다."""
    nl = chr(10)
    _, start, end, _ = locate_json(text)
    body = new_json.strip()
    if start is None:
        return text.rstrip() + nl + nl + "```json" + nl + body + nl + "```" + nl
    before = text[:start]
    fenced = before.endswith(nl) and before.rstrip(nl).split(nl)[-1].lstrip().startswith("```")   # 코드 블록의 안쪽(여는 줄 바로 다음)
    return before + (body + nl if fenced else body) + text[end:]


# kept for older callers/tests
def replace_last_json_block(text: str, new_json: str) -> str:
    return replace_json(text, new_json)


# ---------------------------------------------------------------- 검사 실행

def run(spec: dict | None, text: str) -> dict:
    """{"ok": bool, "problems": [...], "kinds": [...], "json": 값|None}.  kinds: parse(JSON 을 못 읽음) / content(내용 위반)"""
    if not spec:
        return {"ok": True, "problems": [], "kinds": [], "json": None}
    problems: list[str] = []
    kinds: set[str] = set()
    data = None
    if spec.get("json"):
        data, _, err = extract_json(text)
        if err:
            return {"ok": False, "problems": [err], "kinds": ["parse"], "json": None}
        if "schema" in spec:
            _validate(data, spec["schema"], "$", problems)
        for it in spec.get("coverage", []):
            groups = [set(map(_key, _values(data, g))) for g in it["groups"]]
            for v in it["values"]:
                where = [g for g, s in zip(it["groups"], groups) if _key(v) in s]
                if not where:
                    problems.append(f"{v}: {' / '.join(it['groups'])} 어디에도 없습니다(정확히 한 곳에 있어야 합니다).")
                elif len(where) > 1:
                    problems.append(f"{v}: 여러 곳에 있습니다({', '.join(where)}). 정확히 한 곳에만 있어야 합니다.")
        for it in spec.get("refs", []):
            targets = set(map(_key, _values(data, it["to"])))
            for v in _values(data, it["from"]):
                if _key(v) not in targets:
                    problems.append(f"{it['from']} 의 '{v}' 가 {it['to']} 에 없습니다.")
        for it in spec.get("when", []):
            for i, obj in enumerate(_values(data, it["path"])):
                if isinstance(obj, dict) and _matches(obj, it["if"]):
                    for f in it["require"]:
                        if obj.get(f) in (None, "", [], {}):
                            problems.append(f"{it['path']} {i + 1}번째 항목에 '{f}' 가 필요합니다({_cond_text(it['if'])}).")
    if problems:
        kinds.add("content")
    return {"ok": not problems, "problems": problems[:MAX_PROBLEMS] + ([f"… 외 {len(problems) - MAX_PROBLEMS}건"] if len(problems) > MAX_PROBLEMS else []),
            "kinds": sorted(kinds), "json": data}


def _key(v):
    return json.dumps(v, sort_keys=True, ensure_ascii=False) if isinstance(v, (dict, list)) else v


def _cond_text(cond: dict) -> str:
    return ", ".join(f"{k}{'' if not isinstance(v, dict) else ' ' + ' '.join(f'{op} {x}' for op, x in v.items())}{'' if isinstance(v, dict) else ' = ' + str(v)}" for k, v in cond.items())


def _matches(obj: dict, cond: dict) -> bool:
    for k, want in cond.items():
        have = obj.get(k)
        tests = want.items() if isinstance(want, dict) else [("eq", want)]
        for op, x in tests:
            try:
                if have is None or not _OPS[op](have, x):
                    return False
            except TypeError:
                return False
    return True


def _values(data, path: str) -> list:
    """경로가 가리키는 값들의 목록(없는 경로는 빈 목록)."""
    cur = [data]
    for seg in path.split("."):
        each = seg.endswith("[]")
        name = seg[:-2] if each else seg
        nxt = []
        for c in cur:
            v = c.get(name) if isinstance(c, dict) else None
            if v is None:
                continue
            if each:
                if isinstance(v, list):
                    nxt.extend(v)
            else:
                nxt.append(v)
        cur = nxt
    return cur


def _validate(v, schema: dict, where: str, out: list[str]) -> None:
    if len(out) > MAX_PROBLEMS * 3:
        return
    t = schema.get("type")
    if t:
        types = t if isinstance(t, list) else [t]
        if not any(_is(v, x) for x in types):
            out.append(f"{where}: {'/'.join(types)} 여야 합니다(지금: {_name(v)}).")
            return
    if "enum" in schema and v not in schema["enum"]:
        out.append(f"{where}: {json.dumps(v, ensure_ascii=False)} 는 허용값이 아닙니다(허용: {', '.join(json.dumps(x, ensure_ascii=False) for x in schema['enum'])}).")
    if isinstance(v, str):
        if "pattern" in schema and not re.search(schema["pattern"], v):
            out.append(f"{where}: '{v[:40]}' 은(는) 형식({schema['pattern']})에 맞지 않습니다.")
        if "minLength" in schema and len(v) < schema["minLength"]:
            out.append(f"{where}: 글자 수가 {schema['minLength']}자 이상이어야 합니다.")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if "minimum" in schema and v < schema["minimum"]:
            out.append(f"{where}: {v} 은(는) {schema['minimum']} 이상이어야 합니다.")
        if "maximum" in schema and v > schema["maximum"]:
            out.append(f"{where}: {v} 은(는) {schema['maximum']} 이하여야 합니다.")
    if isinstance(v, dict):
        for r in schema.get("required", []):
            if r not in v:
                out.append(f"{where}: 필수 필드 '{r}' 가 없습니다.")
        props = schema.get("properties", {})
        for k, sub in props.items():
            if k in v:
                _validate(v[k], sub, f"{where}.{k}", out)
        if schema.get("additionalProperties") is False:
            for k in v:
                if k not in props:
                    out.append(f"{where}: 정의되지 않은 필드 '{k}' 가 있습니다.")
    if isinstance(v, list):
        if "minItems" in schema and len(v) < schema["minItems"]:
            out.append(f"{where}: 항목이 {schema['minItems']}개 이상이어야 합니다.")
        if "items" in schema:
            for i, x in enumerate(v):
                _validate(x, schema["items"], f"{where}[{i}]", out)


def _is(v, t: str) -> bool:
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    return isinstance(v, _TYPES[t]) and not (t == "array" and False)


def _name(v) -> str:
    return {dict: "object", list: "array", str: "string", bool: "boolean", type(None): "null", int: "integer", float: "number"}.get(type(v), type(v).__name__)
