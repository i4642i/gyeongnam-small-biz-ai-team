"""페이지 본문 조회 도구(format=page_fetch): 주소 하나를 읽어 제목·발행일·본문 앞부분을 돌려준다.

검색 서비스가 붙인 날짜는 틀릴 수 있어서(몇 달 전 기사가 최근 날짜로 표시된 사례), 발행일은 페이지 자체에서 읽는다.
  - 발행일로 「확인」하는 것은 페이지가 스스로 밝힌 값뿐이다: 메타 태그(article:published_time 등)와 JSON-LD 의 datePublished.
  - 주소의 날짜(/2026/09/17/), <time> 태그, 본문 첫머리의 날짜는 「참고」로만 보여 준다.
안전(읽기 전용이지만 서버가 대신 남의 주소를 읽으므로):
  - http(s) 만, 계정 정보가 든 주소 거절, 80·443 포트만.
  - 이름을 풀어 나온 주소가 공인 주소가 아니면(내부망·루프백·링크로컬·예약) 거절. 이동(redirect)할 때마다 다시 검사한다.
  - 응답 1MB 까지만 읽고, 글·HTML·XML·JSON 만 받는다.
  - 환경변수 PAGE_FETCH_ALLOW_PRIVATE=1 은 내부 주소 검사를 끈다(시험용, 운영에서는 쓰지 않는다).
"""

import codecs
import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from html.parser import HTMLParser

MAX_BYTES = 1_000_000
MAX_REDIRECTS = 4
BODY_CHARS = 2500
OK_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "application/xml", "text/xml", "application/json")

# 페이지가 스스로 밝힌 발행일 메타(소문자 name/property/itemprop)
PUB_META = ("article:published_time", "og:article:published_time", "datepublished", "pubdate", "publishdate", "publish-date", "publication_date",
            "dc.date.issued", "dc.date", "dcterms.issued", "dcterms.created", "date", "parsely-pub-date", "sailthru.date", "citation_publication_date",
            "article.published", "article:published")
MOD_META = ("article:modified_time", "og:updated_time", "datemodified", "dcterms.modified", "last-modified", "revised")
URL_DATE = re.compile(r"/((?:19|20)\d\d)[/-]?(0[1-9]|1[0-2])[/-]?(0[1-9]|[12]\d|3[01])(?:/|-|$|\.)")
ISO_DATE = re.compile(r"((?:19|20)\d\d)-(\d\d)-(\d\d)")
MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
TEXT_DATE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+((?:19|20)\d\d)\b")


class FetchError(RuntimeError):
    pass


# 목적 중심 발췌(2026-09-26): focus 가 있으면 본문을 앞에서 자르지 않고, focus·사실(숫자/날짜/조치)이 든 '실제 문장'만 골라 준다.
_OMIT_NOTE = "\n\n※ 원문 전체는 서버에 보관됐으며, 현재 응답에는 지정한 확인 목적과 관련된 부분만 전달되었습니다(핵심 사실·구절 위주)."
_FACT_KW = re.compile(r"(effective|enforce|impose|tariff|duty|sanction|ban|prohibit|restrict|export|import|control|entity\s+list|"
                      r"proclamation|section\s*\d+|quota|licens|percent|deadline|rule|regulation|announc|proposed)", re.I)


def _focus_excerpt(body: str, focus: str, room: int) -> tuple[list[str], list[str]]:
    """(고른 문장들, 확인 못한 focus 토큰). 지어내지 않고 원문 문장만 고른다(검증 가능)."""
    sents = []
    for raw in re.split(r"(?<=[.!?])\s+|\n+", body):
        s = raw.strip()
        if 20 <= len(s) <= 400:
            sents.append(s)
    toks = [t for t in re.findall(r"[A-Za-z]{3,}|\d[\d.,%/-]*", focus or "")
            if t.lower() not in ("and", "the", "for", "with", "date", "rate")]

    def score(s: str) -> int:
        sl = s.lower(); sc = 0
        if re.search(r"\d", s):
            sc += 2                       # 숫자·날짜·비율
        if _FACT_KW.search(s):
            sc += 2                       # 조치·상태 키워드
        for t in toks:
            if t.lower() in sl:
                sc += 3                   # focus 토큰(언어무관 매칭)
        return sc

    scored = sorted(((score(s), i, s) for i, s in enumerate(sents)), key=lambda x: (-x[0], x[1]))
    picked, used = [], 0
    for sc, i, s in scored:
        if sc <= 0:
            break
        if used + len(s) + 4 > room:
            continue
        picked.append((i, s)); used += len(s) + 4
        if used >= room:
            break
    picked.sort()
    passages = [s for _, s in picked]
    not_found = [t for t in toks if not any(t.lower() in s.lower() for s in sents)]
    return passages, not_found


def _allow_private() -> bool:
    return os.environ.get("PAGE_FETCH_ALLOW_PRIVATE") == "1"


def check_url(url: str) -> urllib.parse.ParseResult:
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise FetchError("http:// 또는 https:// 로 시작하는 주소만 읽을 수 있습니다.")
    if p.username or p.password:
        raise FetchError("계정 정보가 들어 있는 주소는 읽지 않습니다.")
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        raise FetchError("포트 번호가 올바르지 않습니다.")
    if not _allow_private() and port not in (80, 443):
        raise FetchError("80·443 이외 포트의 주소는 읽지 않습니다.")
    try:
        infos = socket.getaddrinfo(p.hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise FetchError(f"주소를 찾지 못했습니다: {p.hostname}")
    if not _allow_private():
        for info in infos:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
            if not ip.is_global:
                raise FetchError("내부망·예약 주소는 읽지 않습니다.")
    return p


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _get(url: str, timeout: float) -> tuple[bytes, str, str, str]:
    """(본문 바이트, content-type, 글자 인코딩 힌트, 최종 주소). 이동은 직접 따라가며 매번 검사한다."""
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(MAX_REDIRECTS + 1):
        check_url(url)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; GyeongnamSangkwon/1.0)", "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.5",
                                                   "Accept-Language": "en-US,en;q=0.9,ko;q=0.6"})
        try:
            r = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                continue
            hint = {401: "로그인이 필요한 페이지입니다.", 403: "사이트가 접근을 막았습니다.", 404: "페이지가 없습니다.", 429: "요청 한도를 넘었습니다."}.get(e.code, "")
            raise FetchError(f"페이지가 오류로 답했습니다({e.code}). {hint}".strip())
        except urllib.error.URLError as e:
            raise FetchError(f"페이지에 연결하지 못했습니다: {getattr(e, 'reason', e)}")
        except TimeoutError:
            raise FetchError(f"시간 제한({timeout:g}초)을 넘었습니다.")
        with r:
            ctype = (r.headers.get_content_type() or "").lower()
            if ctype not in OK_TYPES:
                raise FetchError(f"글·HTML·XML·JSON 이 아닌 형식({ctype or '알 수 없음'})은 읽지 않습니다(PDF 등). 검색 결과의 요약이나 다른 출처를 쓰세요.")
            raw = r.read(MAX_BYTES + 1)
            return raw[:MAX_BYTES], ctype, r.headers.get_content_charset() or "", url
    raise FetchError("이동(redirect)이 너무 많습니다.")


def _decode(raw: bytes, charset: str) -> str:
    cands = [charset] if charset else []
    m = re.search(rb'<meta[^>]+charset=["\']?([A-Za-z0-9_\-]+)', raw[:4096], re.I)
    if m:
        cands.append(m.group(1).decode("ascii", "ignore"))
    for c in [*cands, "utf-8"]:
        try:
            codecs.lookup(c)
            return raw.decode(c, "replace")
        except LookupError:
            continue
    return raw.decode("utf-8", "replace")


class _Page(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.metas, self.times, self.jsonld, self.text = "", [], [], [], []
        self._skip, self._in_title, self._ld, self._time = 0, False, False, None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or a.get("http-equiv") or "").strip().lower()
            if key and a.get("content"):
                self.metas.append((key, a["content"].strip()))
        elif tag == "time" and a.get("datetime"):
            self.times.append((a["datetime"].strip(), "pubdate" in a or a.get("itemprop", "").lower() == "datepublished"))
        elif tag == "script" and "ld+json" in a.get("type", "").lower():
            self._ld = True
            self.jsonld.append("")
        elif tag in self.SKIP:
            self._skip += 1
        if tag in ("p", "br", "div", "li", "h1", "h2", "h3", "tr"):
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._ld:
            self._ld = False
        elif tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._ld:
            self.jsonld[-1] += data
        elif not self._skip:
            self.text.append(data)


def _walk(node, key, out):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, str):
                out.append(v)
            _walk(v, key, out)
    elif isinstance(node, list):
        for v in node:
            _walk(v, key, out)


def norm_date(s: str) -> str | None:
    """날짜 글을 YYYY-MM-DD 로. 시각이 붙은 값은 앞의 날짜 부분만 쓴다(시간대 변환은 하지 않는다)."""
    s = (s or "").strip()
    m = ISO_DATE.match(s) or re.match(r"((?:19|20)\d\d)(\d\d)(\d\d)", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = TEXT_DATE.search(s)
        if not m:
            return None
        mo, d, y = MONTHS[m.group(1).lower()[:3]], int(m.group(2)), int(m.group(3))
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def analyze(html: str, url: str) -> dict:
    pg = _Page()
    try:
        pg.feed(html)
        pg.close()
    except Exception:
        pass
    strong, modified, weak = [], [], []
    for k, v in pg.metas:
        d = norm_date(v)
        if d and k in PUB_META:
            strong.append((d, f'meta {k}="{v[:40]}"'))
        elif d and k in MOD_META:
            modified.append((d, f'meta {k}="{v[:40]}"'))
    for blob in pg.jsonld:
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        for key, dest in (("datePublished", strong), ("dateCreated", strong), ("dateModified", modified)):
            vals = []
            _walk(data, key, vals)
            for v in vals:
                d = norm_date(v)
                if d:
                    dest.append((d, f'JSON-LD {key}="{v[:40]}"'))
    for v, flagged in pg.times[:6]:
        d = norm_date(v)
        if d:
            weak.append((d, "<time datetime=\"%s\">%s" % (v[:30], " (발행일 표시)" if flagged else "")))
    m = URL_DATE.search(urllib.parse.urlparse(url).path)
    if m:
        d = norm_date("-".join(m.groups()))
        if d:
            weak.append((d, "주소의 날짜"))
    body = re.sub(r"[ \t\r\f\v]+", " ", "".join(pg.text))
    body = re.sub(r"\n\s*\n+", "\n", body).strip()
    m = TEXT_DATE.search(body[:700])
    if m:
        d = norm_date(m.group(0))
        if d:
            weak.append((d, "본문 첫머리의 날짜"))
    # 같은 (날짜, 근거)는 한 번만
    dedup = lambda xs: list(dict.fromkeys(xs))
    return {"title": re.sub(r"\s+", " ", pg.title).strip()[:200], "confirmed": dedup(strong), "modified": dedup(modified), "weak": dedup(weak), "body": body}


def render(url: str, info: dict, max_chars: int, w: str, focus: str = "") -> tuple[str, str]:
    """(모델에게 줄 글, 확인된 발행일 또는 빈 글). focus 가 있으면 앞부분 절단 대신 관련 문장만 발췌한다."""
    lines = [f"[{w}] 페이지 본문 조회: {url}", f"제목: {info['title'] or '(없음)'}"]
    conf = info["confirmed"]
    first = ""
    if conf:
        first = conf[0][0]
        lines.append(f"발행일(페이지가 밝힌 값): {first}  ← 근거: {conf[0][1]}")
        others = [f"{d} ({b})" for d, b in conf[1:] if d != first]
        if others:
            lines.append("  다른 발행일 표기: " + "; ".join(others[:3]) + "  ※ 서로 다르면 확인된 것으로 보지 말고 사유를 적으세요.")
    else:
        lines.append("발행일: 확인하지 못했습니다(페이지가 발행일을 메타 정보로 밝히지 않음). 아래 참고 날짜만으로는 발행일로 삼지 마세요.")
    if info["modified"]:
        lines.append("수정일: " + "; ".join(f"{d} ({b})" for d, b in info["modified"][:2]))
    if info["weak"]:
        lines.append("참고 날짜(발행일로 확정할 수 없음): " + "; ".join(f"{d} ({b})" for d, b in info["weak"][:4]))
    if focus:
        lines.append(f"확인 목적(focus): {focus[:200]}")
    body_all = info["body"] or ""
    if focus and body_all:
        # 목적 중심 발췌: 앞부분을 자르지 않고 focus·사실이 든 실제 문장만 고른다(지어내기 없음).
        head = "\n".join(lines) + "\n\n관련 구절(원문 발췌):\n"
        room = max(300, min(BODY_CHARS, max_chars - len(head) - 120))
        passages, not_found = _focus_excerpt(body_all, focus, room)
        if passages:
            block = "\n".join(f'- "{p}"' for p in passages)
            if not_found:
                block += "\n확인 못한 항목(not_found): " + ", ".join(not_found[:8])
            note = _OMIT_NOTE if len(body_all) > room else ""
            return head + block + note, first
        # 관련 문장을 못 고르면 기본(앞부분) 발췌로 폴백
    head = "\n".join(lines) + "\n\n본문 앞부분:\n"
    room = max(200, min(BODY_CHARS, max_chars - len(head) - 40))
    body = body_all[:room] + ("…" if len(body_all) > room else "")
    return head + (body or "(본문 글을 찾지 못했습니다)"), first


def fetch(url: str, timeout: float, max_chars: int, w: str, focus: str = "") -> tuple[str, list[dict]]:
    raw, ctype, charset, final = _get(url, timeout)
    text = _decode(raw, charset)
    if ctype == "text/plain":
        info = {"title": "", "confirmed": [], "modified": [], "weak": [], "body": text.strip()}
        m = URL_DATE.search(urllib.parse.urlparse(final).path)
        if m:
            info["weak"].append(("-".join(m.groups()), "주소의 날짜"))
    elif ctype == "application/json":
        info = {"title": "", "confirmed": [], "modified": [], "weak": [], "body": text.strip()}
    else:
        info = analyze(text, final)
    out, pub = render(final, info, max_chars, w, focus)
    return out, [{"w": w, "title": info["title"] or final, "url": final[:500], "date": pub}]
