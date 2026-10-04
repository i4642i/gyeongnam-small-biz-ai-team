"""읽기 전용 보고서 — 직원이 쓴 결과(글 + JSON)를 사람이 읽기 좋은 HTML 한 장으로 바꾼다.

모델을 부르지 않는다(비용 0, 같은 보고서는 늘 같은 모양). 글 부분은 마크다운 → HTML, JSON 은 보고서 종류
(report 칸: changwon_topics·changwon_themes·changwon_health·changwon_picks)에 맞춰 카드·표로, 모르는 종류는
일반 표로 보여 준다. 원본 보고서·JSON 은 건드리지 않는다.
"""

from __future__ import annotations

import html

from markdown_it import MarkdownIt

from app.runs import checks

_md = MarkdownIt("commonmark", {"html": False}).enable("table")

REPORT_NAME = {"changwon_topics": "상권 화제 탐지", "changwon_themes": "상권 테마 연결",
               "changwon_health": "상권 진단", "changwon_picks": "이번 주 상권 리포트"}
GRADE = {"A": ("A 양호", "ok"), "B": ("B 보통", "ok"), "C": ("C 주의", "warn"), "D": ("D 경고", "bad")}


def e(x) -> str:
    return html.escape("" if x is None else str(x))


def badge(v, table=GRADE) -> str:
    label, kind = table.get(str(v), (str(v), "mute")) if v is not None else ("—", "mute")
    return f'<span class="b {kind}">{e(label)}</span>'


def num(x, d=2) -> str:
    if isinstance(x, bool) or x is None:
        return e(x) if x is not None else "—"
    if isinstance(x, (int, float)):
        return f"{x:,.{d}f}".rstrip("0").rstrip(".") if isinstance(x, float) else f"{x:,}"
    return e(x)


def pct(x) -> str:
    return f"{x:.2f}%" if isinstance(x, (int, float)) else "—"


def table(headers: list[str], rows: list[list[str]], cls: str = "") -> str:
    if not rows:
        return '<p class="muted">없음</p>'
    th = "".join(f"<th>{h}</th>" for h in headers)
    tr = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="tbl"><table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>'


def section(title: str, body: str, open_: bool = True) -> str:
    if not body:
        return ""
    return f'<details class="sec"{" open" if open_ else ""}><summary>{e(title)}</summary><div class="sec-body">{body}</div></details>'


def bar(v: float, max_v: float = 1.0) -> str:
    w = max(0.0, min(1.0, (v or 0) / max_v)) * 100 if isinstance(v, (int, float)) else 0
    return f'<span class="bar"><i style="width:{w:.0f}%"></i></span>'


def generic(obj, depth: int = 0) -> str:
    """모르는 구조 — 사전은 이름/값 표, 목록은 줄마다, 깊으면 접는다."""
    if isinstance(obj, dict):
        rows = [[f"<b>{e(k)}</b>", generic(v, depth + 1)] for k, v in obj.items() if not str(k).startswith("_")]
        return table(["항목", "값"], rows, "kv")
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            keys = list(dict.fromkeys(k for x in obj for k in x if not str(k).startswith("_")))[:8]
            return table([e(k) for k in keys], [[generic(x.get(k), depth + 1) for k in keys] for x in obj])
        return ", ".join(generic(x, depth + 1) for x in obj) or "—"
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        return num(obj, 3)
    return e(obj) if obj not in (None, "") else "—"


# ---------------------------------------------------------------- 종류별(상권 보고서 4종)

def _area(x: dict) -> str:
    """동네 이름 + 구(같으면 한 번만)."""
    a, d = x.get("area") or "", x.get("district") or ""
    if a and d and d not in a:
        return f'<b>{e(a)}</b> <span class="muted">{e(d)}</span>'
    return f"<b>{e(a or d)}</b>"


def _article(a: dict) -> str:
    if not isinstance(a, dict):
        return ""
    t = e(a.get("title") or a.get("url") or "")
    url = str(a.get("url") or "")
    link = f'<a href="{e(url)}" target="_blank" rel="noopener">{t}</a>' if url.startswith("http") else t
    meta = " · ".join(e(a.get(k)) for k in ("domain", "published") if a.get(k))
    meta_html = ' <span class="muted">' + meta + '</span>' if meta else ""
    return f"<li>{link}{meta_html}</li>"


def _articles(items) -> str:
    rows = "".join(_article(a) for a in (items or []) if isinstance(a, dict))
    return f'<ul class="arts">{rows}</ul>' if rows else ""


def _topics(d: dict) -> str:
    cands = [c for c in d.get("candidates") or [] if isinstance(c, dict)]
    top = max([c.get("buzz_score") for c in cands if isinstance(c.get("buzz_score"), (int, float))] or [1])
    cards = []
    for c in cands:
        score = c.get("buzz_score")
        sc = f'<span class="score">{bar(score, top)} 화제 점수 {num(score, 2)}</span>' if isinstance(score, (int, float)) else ""
        kind = f'<span class="chip">{e(c.get("event_type"))}</span>' if c.get("event_type") else ""
        cards.append(f'<div class="card"><div class="card-h">{_area(c)} {kind}{sc}</div>'
                     f'<p>{e(c.get("event"))}</p>{_articles(c.get("key_articles"))}</div>')
    out = section(f"화제 후보 {len(cands)}곳", "".join(cards))
    ex = [x for x in d.get("excluded") or [] if isinstance(x, dict)]
    if ex:
        out += section(f"거른 동네 {len(ex)}곳", table(["동네", "거른 이유"], [[_area(x), e(x.get("why"))] for x in ex], "kv"), False)
    return out


def _themes(d: dict) -> str:
    rows = []
    for t in d.get("themes") or []:
        if not isinstance(t, dict):
            continue
        mem = " ".join(f'<span class="chip">{_area(m) if isinstance(m, dict) else e(m)}</span>' for m in t.get("members") or [])
        rows.append([f"<b>{e(t.get('theme'))}</b>", mem or "—"])
    return section(f"상권 테마 {len(rows)}개", table(["테마", "묶인 동네"], rows, "kv"))


def _health(d: dict) -> str:
    facts = [["동네", _area(d)], ["범위", e(d.get("scope"))], ["등급", badge(d.get("grade"))],
             ["점포 수", f'{num(d.get("total_stores"))}개' if d.get("total_stores") is not None else "—"],
             ["기준일", e(d.get("window_end"))]]
    shown = [r for r in facts if r[1] not in ("", "—")]
    reason = "<p><b>등급 근거</b> " + e(d.get("grade_reason")) + "</p>" if d.get("grade_reason") else ""
    out = '<div class="hero">' + table(["항목", "값"], shown, "kv") + reason + "</div>"
    ups = [u for u in d.get("by_upjong") or [] if isinstance(u, dict)]
    top = max([u.get("share_pct") for u in ups if isinstance(u.get("share_pct"), (int, float))] or [1])
    out += section("업종 구성", table(["업종", "점포 수", "비중"],
                   [[e(u.get("name")), num(u.get("count")), f'{bar(u.get("share_pct"), top)} {pct(u.get("share_pct"))}'] for u in ups]))
    if d.get("advice"):
        out += section("소상공인 조언", f'<p>{e(d.get("advice"))}</p>')
    return out


def _picks(d: dict) -> str:
    out = f'<div class="hero"><p class="lead">{e(d.get("headline"))}</p></div>' if d.get("headline") else ""
    cards = []
    for p_ in d.get("picks") or []:
        if not isinstance(p_, dict):
            continue
        theme = f'<span class="chip">{e(p_.get("theme"))}</span>' if p_.get("theme") else ""
        art = _articles([p_.get("key_article")]) if isinstance(p_.get("key_article"), dict) else ""
        grade = badge(p_.get("grade")) if p_.get("grade") else ""
        advice = "<p><b>조언</b> " + e(p_.get("advice")) + "</p>" if p_.get("advice") else ""
        cards.append(f'<div class="card"><div class="card-h">{_area(p_)} {theme} {grade}</div>'
                     f'<p>{e(p_.get("event"))}</p>{advice}{art}</div>')
    return out + section(f"이번 주 동네 {len(cards)}곳", "".join(cards))


RENDER = {"changwon_topics": _topics, "changwon_themes": _themes, "changwon_health": _health, "changwon_picks": _picks}


CSS = """
:root{--bg:#f5f6f8;--s:#fff;--s2:#eef0f3;--ink:#1b1f27;--mu:#5d6572;--ln:#d6dae0;--ok:#1a7f37;--oks:#dcf1e3;--wa:#8a5a00;--was:#fbf0d4;
--bd:#b42318;--bds:#fde8e6;--ac:#2f5bd3;--acs:#e3eafc}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#14171c;--s:#1c2027;--s2:#252a33;--ink:#e6e9ee;--mu:#9aa2b1;--ln:#333a46;
--ok:#56d364;--oks:#15301d;--wa:#e3b341;--was:#352c14;--bd:#ff8a80;--bds:#3a1d1b;--ac:#8fb0ff;--acs:#1c2744}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 "Malgun Gothic","Apple SD Gothic Neo",sans-serif;padding:24px 16px 60px}
.wrap{max-width:1040px;margin:0 auto;display:grid;grid-template-columns:minmax(0,1fr);gap:14px}.wrap>*{min-width:0}h1{font-size:23px;margin:0}h4{margin:4px 0;font-size:15px}p{margin:.35em 0}
.muted{color:var(--mu);font-size:13.5px}.head{display:grid;gap:4px}
.sec{background:var(--s);border:1px solid var(--ln);border-radius:10px}.sec>summary{cursor:pointer;padding:11px 14px;font-weight:700;font-size:16px}
.sec-body{padding:2px 14px 14px}.hero{background:var(--s);border:1px solid var(--ln);border-radius:10px;padding:12px 14px}
.tbl{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--ln);vertical-align:top;overflow-wrap:anywhere}
th{background:var(--s2);font-size:12.5px;color:var(--mu);font-weight:600}table.kv td:first-child{width:28%;white-space:nowrap}
.b{display:inline-block;padding:1px 9px;border-radius:999px;font-size:12.5px;font-weight:700;white-space:nowrap}
.b.ok{background:var(--oks);color:var(--ok)}.b.warn{background:var(--was);color:var(--wa)}.b.bad{background:var(--bds);color:var(--bd)}.b.mute{background:var(--s2);color:var(--mu)}
.chip{display:inline-block;margin:2px;padding:1px 8px;border-radius:6px;font-size:12.5px;border:1px solid var(--ln)}.chip.ok{color:var(--ok)}.chip.bad{color:var(--bd)}
.card{border:1px solid var(--ln);border-radius:9px;padding:10px 12px;margin:8px 0;background:var(--s)}
.bar{display:inline-block;width:70px;height:8px;background:var(--s2);border-radius:4px;vertical-align:middle;overflow:hidden}.bar i{display:block;height:100%;background:var(--ac)}
.ev{list-style:none;padding:0;margin:0}.ev li{padding:7px 0;border-bottom:1px dashed var(--ln)}.id{font-weight:700;font-family:Consolas,monospace;font-size:13px}
.quote{font-size:13.5px;background:var(--s2);border-radius:6px;padding:6px 8px;margin-top:4px;white-space:pre-wrap;overflow-wrap:anywhere}
.md table{margin:8px 0}.md{overflow-wrap:anywhere}.md h2{font-size:18px;margin:14px 0 6px}.md h3{font-size:16px}
a{color:var(--ac)}
.card-h{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;font-size:16px}.card p{margin:.45em 0}
.score{margin-left:auto;font-size:13px;color:var(--mu);white-space:nowrap;display:inline-flex;align-items:center;gap:6px}
.arts{margin:6px 0 0;padding-left:18px;font-size:14px}.arts li{margin:2px 0;overflow-wrap:anywhere}
.lead{font-size:18px;font-weight:700;margin:0}
"""


def render_markdown(md: str, title: str) -> str:
    """마크다운 한 장(회사 최종 보고서 등)을 같은 모양의 HTML 로. 모델을 부르지 않는다."""
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{e(title)}</title><style>{CSS}</style></head><body><div class="wrap"><div class="hero md">{_md.render(md)}</div></div></body></html>')


def _when(iso) -> str:
    """'2026-10-03T15:33:00.208+00:00' → 이 PC 시각 '2026-10-04 00:33'."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(iso)).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(iso or "")


def render(run: dict, agent_name: str = "") -> str:
    text = run.get("answer") or ""
    data, start, end, err = checks.locate_json(text)
    if data is not None and start is not None:
        fence_open = text.rfind("```", 0, start)
        fence_close = text.find("```", end)
        before = text[:fence_open if fence_open != -1 else start]
        after = text[(fence_close + 3) if fence_close != -1 else end:]
    else:
        before, after = text, ""
    kind = data.get("report") if isinstance(data, dict) else None
    title_bits = [REPORT_NAME.get(kind, run.get("task_name") or "보고서")]
    when = _when(run.get("finished_at") or run.get("started_at"))
    head = (f'<div class="head"><h1>{e(" · ".join(title_bits))}</h1>'
            f'<div class="muted">{e(agent_name)} · {e(run.get("task_name"))} · 실행 {e(run.get("id"))} · 작성 {e(when)}'
            f'{" · 상태 " + e(run.get("status")) if run.get("status") != "ok" else ""}</div></div>')
    summary = f'<div class="hero"><p>{e(data.get("summary_ko"))}</p></div>' if isinstance(data, dict) and data.get("summary_ko") else ""
    body = ""
    if isinstance(data, dict):
        body = (RENDER.get(kind) or (lambda d: section("결과", generic(d))))(data)
    prose = section("보고서 본문(A·B 절)", f'<div class="md">{_md.render(before)}</div>') if before.strip() else ""
    tail = section("서버 확인 기록", f'<div class="md">{_md.render(after)}</div>', False) if after.strip() else ""
    warns = (run.get("check") or {}).get("warnings") or []
    warn_html = section(f"경고 {len(warns)}건", "<ul>" + "".join(f"<li>{e(w)}</li>" for w in warns) + "</ul>", False) if warns else ""
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{e(" · ".join(title_bits))}</title><style>{CSS}</style></head><body><div class="wrap">'
            f'{head}{summary}{prose}{body}{tail}{warn_html}</div></body></html>')
