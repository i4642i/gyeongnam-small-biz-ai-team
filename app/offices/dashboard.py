"""회사 대시보드(2026-09-30): 흐름 결과를 카드 + 지도 화면 한 장(HTML)으로 조립한다 — 모델 호출 없음.

숫자(점포 수·업종 비율)는 상권 분석 담당자의 도구 값, 문구는 편집장·테마 담당자 출력을 그대로 쓴다.
좌표는 data/config/regions/<지역>.json 의 coords 에서 온다. 지도 타일은 Esri(키 불필요)라 화면을 보는 컴퓨터가 인터넷에 연결돼 있어야 한다.
"""

from __future__ import annotations

import json
from pathlib import Path

from app import regions

ROOT = Path(__file__).resolve().parents[2]
REGIONS_DIR = ROOT / "data" / "config" / "regions"
REGION_MAP = ROOT / "data" / "config" / "office_region.json"
FALLBACK = (35.2280, 128.6811)   # 창원시청 부근 — 구도 모를 때


def _coords(cfg: dict) -> tuple[dict, dict]:
    c = cfg.get("coords") or {}
    return c.get("areas") or {}, c.get("district_centers") or {}


def _first(outputs: dict, report: str) -> dict | None:
    return next((v for v in outputs.values() if isinstance(v, dict) and v.get("report") == report), None)


def build(flow: dict, theme: str = "") -> str:
    """대시보드 HTML. 편집장 결과(changwon_picks)가 없으면 빈 문자열."""
    out = flow.get("outputs") or {}
    picks = _first(out, "changwon_picks")
    if not picks or not picks.get("picks"):
        return ""
    themes = _first(out, "changwon_themes") or {"themes": []}
    health = {(v.get("area"), v.get("district")): v for v in out.values() if isinstance(v, dict) and v.get("report") == "changwon_health"}
    theme_of = {(m.get("area"), m.get("district")): t.get("theme") for t in themes.get("themes", []) for m in t.get("members", [])}
    _rid, rcfg = regions.of_inputs(flow.get("inputs"))   # 이 실행이 의뢰받은 지역
    areas, centers = _coords(rcfg)
    seen: dict[tuple, int] = {}
    cards = []
    for p in picks["picks"]:
        key = (p.get("area"), p.get("district"))
        h = health.get(key, {})
        ll = areas.get(p.get("area"))
        if ll is None:   # 좌표를 모르는 동네: 구 중심에 두되, 같은 구에서 겹치지 않게 조금씩 비켜 놓는다
            base = tuple(centers.get(p.get("district")) or FALLBACK)
            n = seen[base] = seen.get(base, 0) + 1
            ll = (base[0] + 0.006 * ((n - 1) % 3 - 1), base[1] + 0.008 * ((n - 1) // 3))
        cards.append({**p, "lat": ll[0], "lng": ll[1], "approx": p.get("area") not in areas,
                      "grade": p.get("grade") or h.get("grade", "판단 불가"), "theme": p.get("theme") or theme_of.get(key, "기타"),
                      "grade_reason": h.get("grade_reason", ""), "total": h.get("total_stores"), "scope": h.get("scope"), "focus": h.get("focus"), "mix": (h.get("by_upjong") or [])[:6]})
    data = {"headline": picks.get("headline", ""), "start": picks.get("window_start", ""), "end": picks.get("window_end", ""),
            "cards": cards, "office": flow.get("office_name", ""), "region": regions.scope_text(rcfg, (flow.get("inputs") or {}).get("area"), (flow.get("inputs") or {}).get("upjong")),
            "themes": [{"theme": t.get("theme"), "risk": t.get("risk", ""), "members": [[m.get("area"), m.get("district")] for m in t.get("members", [])]}
                       for t in themes.get("themes", [])]}
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\/")   # </script> 로 끊기지 않게
    if theme == "v3":   # 디자인 시안 v3(흰 바탕·라임). 같은 데이터, 다른 템플릿
        from app.offices.dashboard_v3 import TEMPLATE_V3
        return TEMPLATE_V3.replace("__DATA__", payload)
    return TEMPLATE.replace("__DATA__", payload)


TEMPLATE = r"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>경남 상권 진단 대시보드</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
<style>
:root{--bg:#f4f1ea;--card:#fff;--ink:#22252b;--mut:#6b7280;--line:#e5e1d8;--a:#2f9e44;--b:#3563e9;--c:#f08c00;--d:#e03131;--n:#868e96}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:"Malgun Gothic",system-ui,sans-serif}
.wrap{max-width:1560px;margin:0 auto;padding:24px 16px 56px}
header h1{margin:0 0 4px;font-size:24px}header .sub{color:var(--mut);font-size:14px}
.hero{background:#1f2a44;color:#fff;border-radius:16px;padding:22px 24px;margin:18px 0}
.hero .k{font-size:12px;opacity:.7;letter-spacing:.04em}.hero .h{font-size:20px;font-weight:700;margin-top:6px;line-height:1.45}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px}
.kpi .v{font-size:28px;font-weight:800}.kpi .l{color:var(--mut);font-size:13px}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 10px;align-items:center}
.bar b{font-size:13px;color:var(--mut);margin-right:4px}
.chip{border:1px solid var(--line);background:var(--card);border-radius:99px;padding:6px 13px;font-size:13px;cursor:pointer}
.chip.on{background:#1f2a44;color:#fff;border-color:#1f2a44}
.grid{display:flex;flex-direction:column;gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;display:flex;flex-direction:column;gap:10px;cursor:pointer;transition:border-color .12s,box-shadow .12s}
.card:hover,.card.act{border-color:var(--b);box-shadow:0 2px 10px rgba(31,42,68,.12)}
.more{font-size:12.5px;color:var(--mut)}.more summary{cursor:pointer;color:var(--b);font-weight:700;list-style:none}.more summary::-webkit-details-marker{display:none}
.more[open] summary{margin-bottom:8px}.more .why{margin-bottom:8px}
.pin{transition:transform .12s}.pin.act{transform:rotate(-45deg) scale(1.3);z-index:9}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.area{font-size:18px;font-weight:800;display:flex;align-items:center;gap:8px}
.no{flex:none;min-width:26px;height:26px;padding:0 6px;border-radius:8px;background:#1f2a44;color:#fff;font-size:13px;font-weight:800;display:grid;place-items:center;font-variant-numeric:tabular-nums}.meta{color:var(--mut);font-size:12.5px;margin-top:2px}
.grade{min-width:44px;height:44px;border-radius:12px;color:#fff;font-weight:800;font-size:20px;display:grid;place-items:center}
.gA{background:var(--a)}.gB{background:var(--b)}.gC{background:var(--c)}.gD{background:var(--d)}.gN{background:var(--n);font-size:12px}
.tag{display:inline-block;background:#eef2ff;color:#3b4cca;border-radius:6px;padding:2px 8px;font-size:12px}
.ev{font-size:14px;line-height:1.55}
.mix{display:flex;height:14px;border-radius:7px;overflow:hidden;background:#eee}
.mix i{display:block;height:100%}.legend{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:12px;color:var(--mut)}
.legend s{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px;text-decoration:none}
.adv{background:#fff8e6;border-left:4px solid var(--c);border-radius:8px;padding:10px 12px;font-size:13.5px;line-height:1.55}
.foc{font-size:12.5px;line-height:1.5;border:1px dashed var(--line);border-radius:8px;padding:6px 10px;color:var(--ink)}
.foc b{color:var(--b)}
.adv b{display:block;font-size:12px;color:#a86400;margin-bottom:2px}
.why{font-size:12.5px;color:var(--mut);line-height:1.5} a{color:#3563e9;font-size:13px;text-decoration:none}
.split{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,6fr);gap:16px;margin-top:14px;align-items:start}
.mapcol{position:sticky;top:12px}
#map{height:calc(100vh - 70px);min-height:420px;border-radius:16px;border:1px solid var(--line);z-index:0}
@media(max-width:900px){.split{grid-template-columns:minmax(0,1fr)}.mapcol{position:static;order:-1}#map{height:320px;min-height:0}}
.mapnote{font-size:12px;color:var(--mut);margin-top:6px}
.pin{width:30px;height:30px;border-radius:50% 50% 50% 0;transform:rotate(-45deg);border:2px solid #fff;box-shadow:0 2px 6px rgba(0,0,0,.4)}
.pin span{display:block;transform:rotate(45deg);color:#fff;font-weight:800;font-size:14px;text-align:center;line-height:26px}
.card.hl{outline:3px solid var(--b)}
.sec{margin:26px 0 10px;font-size:16px;font-weight:800}
.themes{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
.th{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;font-size:13.5px;line-height:1.55}.th b{display:block;margin-bottom:6px}
.th .mem{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px}
.th .mem a{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--line);border-radius:99px;padding:2px 10px 2px 3px;font-size:12.5px;color:var(--ink);text-decoration:none;cursor:pointer}
.th .mem a i{font-style:normal;min-width:20px;height:20px;border-radius:99px;background:#1f2a44;color:#fff;font-size:11.5px;font-weight:800;display:grid;place-items:center}
footer{margin-top:28px;color:var(--mut);font-size:12px;line-height:1.6}
</style></head><body><div class="wrap">
<header><h1>경남 상권 진단 대시보드</h1><div class="sub" id="sub"></div></header>
<div class="hero"><div class="k">이번 주 한 줄</div><div class="h" id="head"></div></div>
<div class="kpis" id="kpis"></div>
<div class="bar" id="fd"></div><div class="bar" id="ft"></div>
<div class="split"><div class="grid" id="grid"></div>
<div class="mapcol"><div id="map"></div><div class="mapnote">지도 위치는 동네 부근의 추정 좌표입니다(시안). 핀의 숫자는 목록 번호, 색은 상권 등급입니다. 목록에 마우스를 올리면 핀이 커지고, 카드를 누르면 지도가 그 동네로 이동합니다.</div></div></div>
<div class="sec">테마별 주의 사항</div><div class="themes" id="themes"></div>
<footer>화제와 상권 구성을 함께 본 참고용 정보이며 투자·창업 권유가 아닙니다. 업종 구성은 소상공인시장진흥공단 상가업소정보의 실측 점포 수입니다. 동(읍·면) 이름과 맞는 동네는 그 동의 점포를, 그 밖(공원·시설 일원 등)은 구·시 전체 점포를 셌으며 카드마다 범위를 적었습니다.</footer>
</div><script>
const D=__DATA__;
const $=s=>document.querySelector(s),esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const COL=["#3563e9","#f08c00","#2f9e44","#e03131","#862e9c","#868e96"];
const gc=g=>({A:"gA",B:"gB",C:"gC",D:"gD"}[g]||"gN");
$("#sub").textContent=`${D.region?D.region+" · ":""}${D.office} · 관측 ${D.start} ~ ${D.end}`;$("#head").textContent=D.headline;
const cnt=g=>D.cards.filter(c=>c.grade===g).length;
$("#kpis").innerHTML=[["선정 동네",D.cards.length+"곳"],["A·B (양호)",cnt("A")+cnt("B")+"곳"],["C (주의)",cnt("C")+"곳"],["D (경고)",cnt("D")+"곳"],(new Set(D.cards.map(c=>c.district)).size>1?["구",new Set(D.cards.map(c=>c.district)).size+"개"]:["테마",new Set(D.cards.map(c=>c.theme)).size+"개"])]
 .map(([l,v])=>`<div class="kpi"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("");
const st={d:"전체",t:"전체"};
const GC={A:"#2f9e44",B:"#3563e9",C:"#f08c00",D:"#e03131"};
let map=null,layer=null,markers={};
const pinEl=i=>markers[i]&&markers[i].getElement()?.querySelector(".pin");
function actOn(i,on){const c=document.getElementById("card-"+i);if(c)c.classList.toggle("act",on);const p=pinEl(i);if(p)p.classList.toggle("act",on)}
function focusCard(i,scroll){const el=document.getElementById("card-"+i);if(el&&scroll)el.scrollIntoView({behavior:"smooth",block:"center"});if(el){el.classList.add("hl");setTimeout(()=>el.classList.remove("hl"),1800)}}
function drawMap(list){
 if(typeof L==="undefined"){$("#map").innerHTML='<div class="th">지도를 불러오지 못했습니다(인터넷 연결 확인).</div>';return}
 if(!map){map=L.map("map",{scrollWheelZoom:false,maxZoom:13}).setView([35.20,128.63],11);
  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",{maxZoom:13,attribution:"Tiles &copy; Esri"}).addTo(map);layer=L.layerGroup().addTo(map)}
 layer.clearLayers();markers={};const pts=[];
 list.forEach(c=>{const col=GC[c.grade]||"#7b8494",i=D.cards.indexOf(c);
  const ic=L.divIcon({className:"",html:`<div class="pin" style="background:${col}"><span>${i+1}</span></div>`,iconSize:[30,30],iconAnchor:[15,30]});
  markers[i]=L.marker([c.lat,c.lng],{icon:ic,title:c.area}).addTo(layer).bindTooltip(`${i+1}. ${esc(c.area)} (${esc(c.district)})${c.approx?" · 구 중심 표시":""}`)
   .on("click",()=>focusCard(i,true)).on("mouseover",()=>actOn(i,true)).on("mouseout",()=>actOn(i,false));
  pts.push([c.lat,c.lng])});
 if(pts.length)map.fitBounds(pts,{padding:[40,40],maxZoom:13});setTimeout(()=>map.invalidateSize(),50)}
function chips(el,label,vals,key){$(el).innerHTML=`<b>${label}</b>`+["전체",...vals].map(v=>`<span class="chip ${st[key]===v?"on":""}" data-v="${esc(v)}">${esc(v)}</span>`).join("");
 $(el).onclick=e=>{const v=e.target.dataset?.v;if(v){st[key]=v;draw()}}}
function draw(){
 const dset=[...new Set(D.cards.map(c=>c.district))];$("#fd").hidden=dset.length<2;if(dset.length>1)chips("#fd","구",dset,"d");chips("#ft","테마",[...new Set(D.cards.map(c=>c.theme))],"t");
 const list=D.cards.filter(c=>(st.d==="전체"||c.district===st.d)&&(st.t==="전체"||c.theme===st.t));drawMap(list);
 $("#grid").innerHTML=list.map(c=>{
  const a=c.key_article||{};
  const mix=(c.mix||[]).map((m,i)=>`<i style="width:${m.share_pct}%;background:${COL[i]}" title="${esc(m.name)} ${m.share_pct}%"></i>`).join("");
  const lg=(c.mix||[]).slice(0,4).map((m,i)=>`<span><s style="background:${COL[i]}"></s>${esc(m.name)} ${m.share_pct}%</span>`).join("");
  return `<div class="card" id="card-${D.cards.indexOf(c)}"><div class="top"><div><div class="area"><span class="no">${D.cards.indexOf(c)+1}</span>${esc(c.area)}</div><div class="meta">${esc(c.district)} · <span class="tag">${esc(c.theme)}</span></div></div><div class="grade ${gc(c.grade)}">${esc(c.grade)}</div></div>
  <div class="ev">${esc(c.event)}</div>
  ${mix?`<div><div class="mix">${mix}</div><div class="legend" style="margin-top:6px">${lg}<span>· ${c.scope==="동 단위"?esc(c.area)+" 점포":esc(c.district)+" 전체 점포"} ${Number(c.total||0).toLocaleString()}개</span></div></div>`:""}
  ${c.focus&&c.focus.count!=null?`<div class="foc"><b>${esc(c.focus.upjong)}</b> ${Number(c.focus.count).toLocaleString()}개 · ${c.focus.share_pct}%${c.focus.vs_wide?` (시·구 평균 ${c.focus.wide_share_pct}%의 ${c.focus.vs_wide}배)`:""}${(c.focus.detail||[]).length?" · 상위 "+c.focus.detail.slice(0,3).map(d=>esc(d.name)+" "+d.count).join(", "):""}</div>`:""}
  <div class="adv"><b>소상공인 조언</b>${esc(c.advice)}</div>
  <details class="more"><summary>등급 근거·대표 기사 보기</summary><div class="why">등급 근거: ${esc(c.grade_reason)}</div>
  ${a.url?`<a href="${esc(a.url)}" target="_blank" rel="noopener">📰 ${esc(a.domain)} · ${esc(a.published)}</a>`:""}</details></div>`}).join("")||`<div class="th">조건에 맞는 동네가 없습니다.</div>`;
 document.querySelectorAll(".card").forEach(el=>{const i=+el.id.slice(5);
  el.onmouseenter=()=>actOn(i,true);el.onmouseleave=()=>actOn(i,false);
  el.onclick=e=>{if(e.target.closest("a,summary"))return;const c=D.cards[i];if(map&&markers[i]){map.flyTo([c.lat,c.lng],Math.max(map.getZoom(),12),{duration:.5});markers[i].openTooltip()}}});
}
draw();
// 테마에 속한 동네는 최종 선정 카드가 가진 테마로 잇는다(테마 담당자 출력에 동네 이름이 빠진 실행도 있다)
$("#themes").innerHTML=D.themes.map(t=>{
 const mem=D.cards.map((c,i)=>[i,c]).filter(([,c])=>c.theme===t.theme).map(([i,c])=>`<a data-go="${i}"><i>${i+1}</i>${esc(c.area)}</a>`).join("");
 return `<div class="th"><b>${esc(t.theme)}</b>${mem?`<div class="mem">${mem}</div>`:""}${t.risk?esc(t.risk):(mem?"":'<span style="color:var(--mut)">이번 선정에 해당하는 동네가 없습니다.</span>')}</div>`}).join("");
$("#themes").onclick=e=>{const a=e.target.closest("[data-go]");if(!a)return;const el=document.getElementById("card-"+a.dataset.go);if(el)focusCard(+a.dataset.go,true)};
</script></body></html>"""
