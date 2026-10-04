"""문서를 검색하기 좋은 크기의 조각으로 자른다.

조각은 쪽 경계를 넘지 않는다 → 조각마다 정확한 쪽수를 붙일 수 있어 출처를 "p.3"처럼 보여 줄 수 있다.
크기 700자/겹침 100자는 이 PC에서 임베딩 속도(조각당 약 0.7초)를 잰 값과 같은 설정이다.

구조가 있는 마크다운(제목 '#'이 있는 문서 — 공시·녹취록을 MD로 바꿔 올린 경우, 2026-09-22 추가)은
따로 다룬다: 제목 단위로 구역을 나누고(구역을 넘어 조각이 이어지지 않음), 표(연속된 '|...|' 줄)는
작으면 통째로, 크면 행 단위로 나누되 조각마다 표 머리(열 날짜)·단위 줄을 반복하며(2026-09-28, 아래 '표' 절),
문서 맨 앞의 정보표(티커·문서 종류·기간)를 읽어 조각마다 "[티커 · 문서 종류 · 기간 · 구역 제목]" 머리표를
붙인다. 제목이 없어도 맨 앞 정보표가 있는 공시 MD 는 이 경로를 탄다. 그 밖의 보통 문서(PDF·평문 등)는
기존 방식 그대로다(회귀 없음).
"""

import json
import re

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.rag.extract import Page

CHUNK_SIZE = 700
CHUNK_OVERLAP = 100
MIN_CHUNK_CHARS = 15    # 공백을 뺀 글자 수가 이보다 적은 조각(쪽 번호만 있는 쪽 등)은 버린다

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP, separators=["\n\n", "\n", ". ", " ", ""],
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
# 어떤 키든 다 받는다(게재일 근거·원문·통화 날짜 등 새 줄이 계속 늘어날 수 있어 고정 목록으로 안 막는다).
# "---|---"·"항목|값" 같은 표 장식 줄도 걸리지만 doc_meta() 가 아는 키만 골라 쓰므로 해가 없다.
_FRONT_MATTER_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|\s*$", re.M)
_FRONT_MATTER_END = re.compile(r"\n-{3,}\s*\n")   # 정보표 끝(구분선) — 이 뒤 본문에 있는 다른 표는 정보표로 안 읽는다
# 10-K 는 분량이 커서(수십만~백만 자) 리서치 가치가 낮은 절(자산 내역·소송·경영진 보수 등 지배구조·부속서류)은
# 뺀다(2026-09-22, 임베딩이 너무 느리다는 지적 반영). "남길 목록"이 아니라 "뺄 목록"으로 짠다 — 제목 인식이
# 완벽하지 않아(SEC 필링마다 HTML 구조가 달라 Item 5·7처럼 못 잡는 경우가 실제로 있었다) 어느 절에 속하는지
# 애매하면 무조건 남긴다(잘못 지우는 것보다 덜 지우는 게 안전). Item 10/11 처럼 "1"로 시작하는 다른 번호와
# 안 겹치게 마침표까지 포함해서 비교한다.
TENK_DROP_ITEMS = ("item 2.", "item 3.", "item 4.", "item 5.", "item 9a.", "item 9b.", "item 9c.",
                    "item 10.", "item 11.", "item 12.", "item 13.", "item 14.", "item 15.", "item 16.")


def _parse_front_matter(text: str) -> dict:
    """문서 맨 앞 정보표(티커·회사·문서 종류·기간·게재일·게재일 근거·원문·통화 날짜 등)를 읽는다. 없으면 빈 dict.
    표 전체(구분선 '---' 앞까지, 없으면 안전하게 4000자까지)를 본다 — 접수번호·URL이 들어가는 줄은
    600자를 넘길 수 있어서(2026-09-23) 앞부분만 보던 예전 방식을 넓혔다."""
    end = _FRONT_MATTER_END.search(text)
    head = text[: end.start()] if end else text[:4000]
    return {m.group(1): m.group(2) for m in _FRONT_MATTER_ROW.finditer(head)}


def doc_meta(text: str) -> dict:
    """문서 하나(쪽 여러 개면 첫 쪽)의 정보표를 문서 메타데이터로 바꾼다. 없으면 빈 dict —
    app/rag/worker.py 가 documents 테이블에 저장해 RAG 개선 4번(티커·문서 종류 거르기)과 게재일 확보에 쓴다.

    date_status: "confirmed"(게재일을 정확히 읽음) / "needs_review"(게재일 줄은 있는데 못 읽음 — 여러 후보·충돌 등)
                 / "unknown"(게재일 줄 자체가 없음, 아직 확인 시도 전). 문서 처리 상태(status: queued/ready/failed 등)와는
                 별개로 둔다(2026-09-23) — 날짜 확인 여부가 문서 처리 성공 여부에 영향을 주지 않게 하기 위해서.
    event_date: 게재일과 다를 수 있는 "그 안의 사건 날짜"(예: 실적 발표 통화 날짜) — 있으면만, 기준일 검색에는 안 쓴다."""
    fm = _parse_front_matter(text)
    out = {}
    if fm.get("티커"):
        out["ticker"] = fm["티커"]
    if fm.get("문서 종류"):
        out["doc_type"] = fm["문서 종류"]
    if fm.get("기간"):
        out["period"] = fm["기간"]
    from app.rag.scope import iso_date
    out["published_at"] = None
    out["event_date"] = None
    if fm.get("통화 날짜"):
        try:
            out["event_date"] = iso_date(fm["통화 날짜"])
        except ValueError:
            pass
    raw = fm.get("게재일")
    if raw is None:
        out["date_status"] = "unknown"
    else:
        try:
            out["published_at"] = iso_date(raw)
            out["date_status"] = "confirmed"
        except ValueError:
            out["date_status"] = "needs_review"   # 값은 있지만 정확한 YYYY-MM-DD 가 아님(예: "확인 필요", "2026-08-?")
    basis_raw = (fm.get("게재일 근거") or "").strip()
    evidence = {}
    if basis_raw:
        evidence["basis_text"] = basis_raw
        if "SEC" in basis_raw.upper():
            out["date_basis"] = "sec_filing_date"
        elif out["date_status"] == "confirmed":
            out["date_basis"] = "document_stated"
    elif out["date_status"] == "confirmed":
        out["date_basis"] = "document_stated"   # 근거 줄이 없어도 문서 자체에 정확한 날짜가 있으면 이 값
    if fm.get("원문"):
        evidence["source_url"] = fm["원문"]
    if fm.get("접수번호"):   # 중복 등록 판단(접수번호+문서파일)에 쓴다 — 문장에서 다시 뽑지 않고 이 값을 그대로 쓴다
        evidence["accession"] = fm["접수번호"]
    if fm.get("문서파일"):
        evidence["document_filename"] = fm["문서파일"]
    if evidence:
        out["date_evidence"] = json.dumps(evidence, ensure_ascii=False)
    return out


def _split_by_heading(text: str) -> list[tuple[str, str]]:
    """[(구역 제목, 구역 글)] — '#'로 시작하는 줄마다 구역을 나눈다. 첫 제목 앞 글은 제목 ''(정보표 등)."""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [("", text)]
    sections = []
    if matches[0].start() > 0:
        sections.append(("", text[: matches[0].start()]))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((m.group(2).strip(), text[start:end]))
    return sections


# ---------------------------------------------------------------- 표 (2026-09-28 개정)
# 예전: 표 블록을 통째로 한 조각. 문제 ① 큰 표(대차대조표 수천 자)는 임베딩 한도(512토큰)에서 아래쪽이 잘려
# 검색에 안 걸림 ② 제목 없는 공시 MD 는 이 경로를 아예 안 타서 700자 일반 분할로 표가 잘리고, 두 번째 조각부터
# 열 머리(날짜)·단위가 사라짐 — 숫자를 조각과 대조할 때 기간·단위를 확인할 수 없었다(⑥ 변수 방식, 숙제 A′-1).
# 지금: 작은 표는 통째로, 큰 표는 행 단위로 나누되 조각마다 "표 앞 단위·제목 줄 + 표 머리 줄"을 반복한다.
# 머리에 기간 표시가 없는 이어진 표(페이지 경계로 HTML 표가 끊긴 것)는 바로 앞 표의 머리를 이어받는다.
TABLE_WHOLE_MAX = 1400          # 이보다 짧은 표는 통째로 한 조각
TABLE_PIECE_TARGET = 900        # 큰 표를 나눌 때 조각당 목표 글자 수(머리 포함)
TABLE_HEADER_MAX_ROWS = 8
_NUM_CELL = re.compile(r"^\(?-?\$?\s*\d[\d,]*(?:\.\d+)?\s*\)?\s*%?$")
_YEAR_CELL = re.compile(r"^(?:19|20)\d{2}$")
_SEP_ROW = re.compile(r"^\s*\|(?:\s*:?-{3,}:?\s*\|)+\s*$")
_PERIOD_HINT = re.compile(r"(january|february|march|april|may|june|july|august|september|october|november|december"
                          r"|months? ended|year ended|weeks? ended|quarter|fiscal|\b(?:19|20)\d{2}\b)", re.I)
_UNITS_HINT = re.compile(r"(in (?:thousands|millions|billions)|thousands of|millions of|except per share|\(unaudited\))", re.I)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_empty_row(line: str) -> bool:
    return not any(c for c in _cells(line)) and not _SEP_ROW.match(line)


def _is_data_row(line: str) -> bool:
    """첫 칸에 이름이 있고 뒤 칸에 숫자(연도만 있는 칸은 제외)가 있는 줄 = 값 줄. 그 앞 줄들이 표 머리."""
    c = _cells(line)
    if not c or not c[0] or _SEP_ROW.match(line):
        return False
    return any(x and _NUM_CELL.match(x) and not _YEAR_CELL.match(x.strip("()$ ")) for x in c[1:])


def _ncols(lines: list[str]) -> int:
    return max((len(_cells(l)) for l in lines[:3]), default=0)


def _table_pieces(lines: list[str], context: list[str], prev_header: list[str] | None) -> tuple[list[str], list[str]]:
    """표 한 블록 → (조각 글 목록, 다음 표가 이어받을 수 있는 머리)."""
    rows = [l for l in lines if not _is_empty_row(l)]
    first = next((i for i, l in enumerate(rows) if _is_data_row(l)), len(rows))
    cut = min(first, TABLE_HEADER_MAX_ROWS)
    # 구분선(|---|) 뒤 줄은 날짜·단위 줄(첫 칸이 비었거나 기간·단위 표기)일 때만 머리로 본다 — 숫자 없는 글 표(지식 표 등)에서
    # 앞 데이터 줄 여러 개가 조각마다 머리로 반복되던 문제(2026-09-29)
    sep = next((i for i, l in enumerate(rows[:cut]) if _SEP_ROW.match(l)), None)
    if sep is not None:
        j = sep + 1
        while j < cut and (not _cells(rows[j])[0] or _PERIOD_HINT.search(rows[j]) or _UNITS_HINT.search(rows[j])):
            j += 1
        cut = j
    header, body = rows[:cut], rows[cut:]
    has_period = bool(_PERIOD_HINT.search("\n".join(header)))
    if not has_period and prev_header and _ncols(prev_header) == _ncols(rows):
        header = ["(앞 표의 머리를 이어받음)"] + prev_header + header
    carry = header if has_period else (prev_header or [])
    head = "\n".join(context + header)
    whole = "\n".join(context + header + body)
    if len(whole) <= TABLE_WHOLE_MAX or not body:
        return [whole], carry
    out, buf, size = [], [], len(head)
    for r in body:
        if buf and size + len(r) + 1 > TABLE_PIECE_TARGET:
            out.append(head + "\n" + "\n".join(buf))
            buf, size = [], len(head)
        buf.append(r)
        size += len(r) + 1
    if buf:
        out.append(head + "\n" + "\n".join(buf))
    return out, carry


def _table_context(text_block: str) -> list[str]:
    """표 바로 앞 글의 마지막 짧은 줄들 중 단위·제목으로 보이는 것(최대 2줄)."""
    tail = [l.strip() for l in text_block.strip().split("\n") if l.strip()][-3:]
    keep = [l for l in tail if len(l) <= 200 and (_UNITS_HINT.search(l) or l.startswith(("#", "**")) or l.isupper())]
    return keep[-2:]


def _split_keep_tables(text: str) -> list[str]:
    """표(연속된 '|...|' 줄 묶음)는 _table_pieces 로(작으면 통째로, 크면 머리를 반복하며 행 단위로),
    나머지 글은 기존 재귀 분할기로 나눈다."""
    blocks: list[tuple[bool, list[str]]] = []
    buf: list[str] = []
    in_table = False
    for line in text.split("\n"):
        is_table_line = bool(_TABLE_LINE_RE.match(line))
        if is_table_line != in_table:
            if buf:
                blocks.append((in_table, buf))
            buf, in_table = [], is_table_line
        buf.append(line)
    if buf:
        blocks.append((in_table, buf))

    out: list[str] = []
    prev_text, prev_header = "", None
    for is_table, buf_lines in blocks:
        block = "\n".join(buf_lines).strip("\n")
        if not block.strip():
            continue
        if is_table:
            pieces, prev_header = _table_pieces([l.rstrip("\r") for l in buf_lines if l.strip()],
                                                _table_context(prev_text), prev_header)
            out.extend(pieces)
        else:
            out.extend(_splitter.split_text(block))
            prev_text = block
            if len(block.strip()) > 400:   # 표 사이에 긴 본문이 끼면 다른 표 — 머리를 이어받지 않는다
                prev_header = None
    return out


def make_chunks(pages: list[Page]) -> list[tuple[int | None, str]]:
    """[(쪽 번호 또는 None, 조각 글)] — 문서 순서대로."""
    out = []
    for page in pages:
        text = page.text
        # 제목('#')이 없어도 공시 MD(맨 앞 정보표)는 구조 경로로 — 제목 인식에 실패한 공시가 많았다(2026-09-28 실측:
        # 색인 컴퓨터 34개 중 24개). 그 경우 표가 700자 일반 분할로 잘려 열 머리가 사라졌다.
        if not _HEADING_RE.search(text) and not text.lstrip().startswith("| 항목 | 값 |"):
            for piece in _splitter.split_text(text):
                piece = piece.strip()
                if len(re.sub(r"\s", "", piece)) >= MIN_CHUNK_CHARS:
                    out.append((page.number, piece))
            continue

        meta = _parse_front_matter(text)
        tag_parts = [meta[k] for k in ("티커", "문서 종류", "기간") if meta.get(k)]
        is_10k = meta.get("문서 종류", "").strip().upper() == "10-K"
        first = _HEADING_RE.search(text)
        doc_title = first.group(2).strip() if first else ""
        for title, body in _split_by_heading(text):
            if is_10k and title.strip().lower().startswith(TENK_DROP_ITEMS):
                continue   # 10-K 리서치 가치 낮은 절(자산·소송·지배구조·부속서류 등)은 버림 — 애매하면 남김
            if tag_parts:
                prefix = f"[{' · '.join(tag_parts + [title or '(개요)'])}]\n"
            elif title:
                # 정보표 없는 안내 문서(직원 전문자료 등, 2026-09-29): 문서 제목·절 제목을 붙인다 — 절 제목 없이 본문 목록만
                # 남은 조각은 질문과 맞아도 순위 모델 점수가 기준에 못 미쳤다("2차 효과를 인정하는 조건" 절)
                prefix = f"[{doc_title} · {title}]\n" if doc_title and title != doc_title else f"[{title}]\n"
            else:
                prefix = ""
            for piece in _split_keep_tables(body):
                piece = piece.strip()
                if len(re.sub(r"\s", "", piece)) >= MIN_CHUNK_CHARS:
                    out.append((page.number, prefix + piece))
    return out
