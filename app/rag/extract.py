"""문서에서 글을 뽑는다. PDF / 텍스트 / 마크다운.

PDF 는 pymupdf4llm 으로 쪽마다 마크다운으로 뽑아 표·제목 구조를 살리고(100쪽에 약 30초),
실패하면 PyMuPDF 의 일반 추출로 대신한다(100쪽에 약 0.2초).
텍스트가 거의 없는 쪽(스캔 이미지 등)은 empty_pages 로 세어 알려 준다 — OCR 은 아직 지원하지 않는다.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("agent_town.extract")

SUPPORTED = {".pdf", ".txt", ".md", ".markdown"}
MIN_PAGE_CHARS = 10   # 이보다 짧은 쪽은 '글이 없는 쪽'으로 본다


class ExtractError(Exception):
    """사용자에게 그대로 보여줘도 되는 문서 읽기 실패 사유."""


@dataclass
class Page:
    number: int | None   # PDF 는 1부터, 텍스트 파일은 None
    text: str


def _decode(data: bytes) -> str:
    # 한국어 윈도우 텍스트 파일은 UTF-8 이 아니라 CP949 인 경우가 많다
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _pdf(path: Path) -> list[Page]:
    import fitz  # PyMuPDF

    try:
        doc = fitz.open(path)
    except Exception:
        raise ExtractError("PDF 파일을 열 수 없습니다. 손상되었거나 PDF가 아닐 수 있습니다.")
    try:
        if doc.needs_pass:
            raise ExtractError("암호가 걸린 PDF는 읽을 수 없습니다. 암호를 풀어서 올려 주세요.")
        texts: list[str] | None = None
        try:
            import pymupdf4llm

            chunks = pymupdf4llm.to_markdown(doc, page_chunks=True, show_progress=False)
            if len(chunks) == doc.page_count:
                texts = [c["text"] for c in chunks]
        except Exception:
            log.warning("pymupdf4llm 추출 실패 → 일반 추출로 대체", exc_info=True)
        if texts is None:
            texts = [page.get_text() for page in doc]
        return [Page(i + 1, t) for i, t in enumerate(texts)]
    finally:
        doc.close()


def extract(path: Path, ext: str) -> tuple[list[Page], int]:
    """(쪽 목록, 글이 없는 쪽 수). 글을 하나도 못 뽑으면 ExtractError."""
    if ext == ".pdf":
        pages = _pdf(path)
    elif ext in (".txt", ".md", ".markdown"):
        pages = [Page(None, _decode(path.read_bytes()))]
    else:
        raise ExtractError(f"지원하지 않는 형식입니다: {ext}")
    empty = sum(1 for p in pages if len(p.text.strip()) < MIN_PAGE_CHARS)
    if empty == len(pages):
        raise ExtractError(
            "문서에서 글을 찾을 수 없습니다. 스캔한 이미지 PDF일 수 있습니다(OCR은 아직 지원하지 않습니다)."
            if ext == ".pdf" else "문서가 비어 있습니다."
        )
    return pages, empty
