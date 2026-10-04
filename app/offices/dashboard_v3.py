"""대시보드 시안 v3(2026-10-01) — 상담소 v3 와 같은 말투: 흰 바탕·검정·라임 하나, 얇은 큰 제목, 고정폭 라벨, 1px 테두리.

dashboard.build(flow, theme="v3") 가 이 템플릿을 쓴다. 데이터는 기존과 같다(카드 목록·테마). 달라진 점:
같은 동네의 화제는 카드 하나로 묶고, 업종 구성은 한 번만 그린다. 숫자 타일은 3칸(동네·테마·등급 분포),
업종 막대는 핵심 업종만 검정에 나머지는 회색, 시·구 평균 위치에 세로선. 페이지 하나로 스크롤(부모 화면이 iframe 높이를 맞춘다).
"""

TEMPLATE_V3 = r"""<!doctype html><html lang="ko" data-v3><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>경남 상권 진단 대시보드</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+KR:wght@400;500;700&display=swap">
<style>
:root{--ink:#111;--mut:#555;--faint:#777;--line:#e4e4e4;--line2:#ddd;--lime:#b8f36b;--mono:"IBM Plex Mono","IBM Plex Sans KR",ui-monospace,monospace}
*{box-sizing:border-box}
html,body{margin:0;background:transparent;color:var(--ink);font-family:"IBM Plex Sans KR","Noto Sans KR","Malgun Gothic",system-ui,sans-serif;font-size:15px;line-height:1.55;word-break:keep-all;overflow-wrap:break-word}
.wrap{max-width:1560px;margin:0 auto;padding:2px 4px 8px}
@media(min-width:901px){html,body{height:100%;overflow:hidden}.wrap{height:100vh;display:flex;flex-direction:column}.wrap>*{flex:none}.wrap>.split{flex:1 1 0;min-height:0}}
.sub{color:var(--mut);font-size:13px;margin:0 0 8px;background:rgba(255,255,255,.72);display:inline-block;padding:2px 10px;border-radius:8px}
.lab{background:#fff;display:inline-block;font-family:var(--mono);font-size:12px;color:var(--mut);border:1px solid var(--line2);border-radius:8px;padding:2px 9px}
.hero{margin:0 0 12px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}.hero h2{margin:0;font-size:clamp(20px,1.9vw,28px);line-height:1.3;font-weight:400;letter-spacing:-.025em}
.kpis{display:grid;grid-template-columns:1fr 1fr 2fr;gap:10px;margin-bottom:10px}
.kpi{background:#fff;border:1px solid var(--line);border-radius:14px;padding:8px 16px}
.kpi .l{font-size:12.5px;color:var(--mut)}.kpi .v{font-size:24px;font-weight:400;letter-spacing:-.03em;line-height:1.2}
.dist{display:flex;height:10px;border-radius:5px;overflow:hidden;background:#eee;margin:6px 0 6px;border:1px solid var(--line2)}
.dist i{display:block;height:100%}
.distl{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;color:var(--ink)}.distl s{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;border:1px solid #111;text-decoration:none;vertical-align:-1px}
.bar{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 8px;align-items:center}.bar[hidden]{display:none}
.bar b{font-size:13px;color:var(--mut);margin-right:4px;font-weight:500}
.chip{border:1px solid var(--ink);background:#fff;border-radius:8px;padding:3px 10px;font-size:13px;cursor:pointer;font-family:inherit;color:var(--ink)}
.chip:hover{background:#f2f2f2}.chip.on{background:var(--ink);color:#fff}
.split{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,6fr);gap:16px;margin-top:4px;align-items:start}
.list{display:flex;flex-direction:column;gap:10px;background:rgba(255,255,255,.82);border-radius:18px}.cards{display:flex;flex-direction:column;gap:10px}
.mapcol{height:100%}
.pp #map{width:186mm!important;height:236mm!important}
.mapttl,.maplegend{display:none}.mapttl{margin:0 0 8px;font-size:18px;font-weight:500}.maplegend{margin:8px 0 0;font-size:12.5px;color:#555}
#map{height:460px;border-radius:18px;border:1px solid var(--line);z-index:0}
@media(min-width:901px){.split{align-items:stretch}.list{overflow-y:auto;height:100%;padding:12px 10px 14px 12px}.mapcol{height:100%}#map{height:100%}}
.card{border:1px solid var(--line);border-radius:16px;padding:16px 18px;display:flex;flex-direction:column;gap:12px;cursor:pointer;background:#fff}
.card:hover,.card.act{border-color:var(--ink)}.card.hl{outline:2px solid var(--ink)}
.top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.ttl{display:flex;gap:10px;align-items:flex-start}
.no{flex:none;min-width:26px;height:26px;padding:0 6px;border-radius:7px;background:var(--ink);color:#fff;font-family:var(--mono);font-size:13px;font-weight:500;display:grid;place-items:center;margin-top:3px}
h3{margin:0;font-size:18px;font-weight:500;line-height:1.3}.meta{color:var(--mut);font-size:13px;margin-top:2px}
.tag{display:inline-block;border:1px solid var(--line2);border-radius:6px;padding:0 7px;font-size:12px;margin-right:4px;margin-top:2px}
.grade{flex:none;font-size:13px;font-weight:500;border-radius:8px;padding:4px 10px;border:1px solid var(--ink);white-space:nowrap}
.gA,.gB{background:var(--lime);color:#111}.gC{background:#fff;color:#111}.gD{background:var(--ink);color:#fff}.gN{background:#fff;color:var(--mut);border-color:var(--line2)}
.ev{font-size:14.5px;line-height:1.6}.evn{display:inline-block;font-family:var(--mono);font-size:12px;border:1px solid var(--line2);border-radius:6px;padding:0 7px;margin-right:8px;color:var(--mut)}
.mixw{position:relative}.mixw.hasavg{padding-top:22px}
.avgl{position:absolute;top:0;transform:translateX(-50%);font-size:12px;line-height:1;white-space:nowrap;background:#fff;border:1px solid #111;border-radius:6px;padding:3px 7px;font-family:var(--mono)}
.avgl::after{content:"";position:absolute;left:50%;bottom:-5px;width:7px;height:7px;background:#fff;border-right:1px solid #111;border-bottom:1px solid #111;transform:translateX(-50%) rotate(45deg)}
.mix{position:relative;display:flex;height:14px;border-radius:7px;overflow:hidden;background:#f0f0f0}
.mix i{display:block;height:100%}
.avg{position:absolute;top:18px;bottom:auto;height:22px;width:0;border-left:2px solid #111}
.mixl{display:flex;flex-wrap:wrap;gap:3px 14px;font-size:13px;color:var(--mut);margin-top:8px}.mixl b{color:var(--ink);font-weight:700}
.mixl s{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;text-decoration:none;vertical-align:-1px}
.foc{font-size:13.5px;line-height:1.55;border:1px solid var(--line);border-radius:10px;padding:8px 12px}
.adv{border-left:3px solid var(--ink);padding:2px 0 2px 12px;font-size:14px;line-height:1.6}
.adv b{display:block;font-size:12px;font-weight:500;color:var(--mut);font-family:var(--mono);margin-bottom:2px}
.more{font-size:13px;color:var(--mut)}.more summary{cursor:pointer;color:var(--ink);font-weight:500;list-style:none}.more summary::-webkit-details-marker{display:none}
.more summary::after{content:" +";font-family:var(--mono)}.more[open] summary::after{content:" −"}.more[open] summary{margin-bottom:8px}
.more p{margin:0 0 8px;line-height:1.6}.more a{display:block;color:var(--ink);font-size:13px;margin-top:4px}
.pin{width:30px;height:30px;border-radius:9px;border:1.5px solid #111;display:grid;place-items:center;font-family:var(--mono);font-weight:500;font-size:13px;background:#fff;color:#111;transition:transform .12s}
.pin.pA,.pin.pB{background:var(--lime)}.pin.pD{background:#111;color:#fff}.pin.act{transform:scale(1.25)}
.sec{margin:26px 0 10px;font-size:20px;font-weight:400;letter-spacing:-.02em}
.themes{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}
.th{background:#fff;border:1px solid var(--line);border-radius:16px;padding:14px 18px;font-size:14px;line-height:1.6}.th b{display:block;font-size:15px;font-weight:500;margin-bottom:6px}
.th .mem{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}
.th .mem a{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--ink);border-radius:8px;padding:2px 10px 2px 3px;font-size:13px;color:var(--ink);cursor:pointer}
.th .mem a i{font-style:normal;min-width:20px;height:20px;border-radius:5px;background:var(--ink);color:#fff;font-family:var(--mono);font-size:11.5px;display:grid;place-items:center}
footer{background:rgba(255,255,255,.8);padding:10px 14px;border-radius:12px;margin-top:22px;color:#555;font-size:13px;line-height:1.7}
@media print{
 @page{size:A4;margin:12mm}
 *{-webkit-print-color-adjust:exact;print-color-adjust:exact}
 html,body{height:auto!important;overflow:visible!important;background:#fff!important}
 .wrap{height:auto!important;display:block!important;max-width:none;padding:0}
 .wrap>*{flex:none}
 .split{display:block!important}.leaflet-control-zoom{display:none}
 .list{overflow:visible!important;height:auto!important;background:none!important;padding:0!important;display:block}
 .cards{display:block}.card{break-inside:avoid;margin-bottom:10px}
 .mapcol{height:auto!important;margin:0;break-before:page;page-break-before:always;break-inside:avoid;transform:none!important}
 .mapttl,.maplegend{display:block!important}
 #map,.pp #map{width:186mm!important;height:236mm!important;border-radius:6px;border:1px solid #111}
 .kpis{grid-template-columns:1fr 1fr 2fr!important}
 .bar,.more summary{display:none}
 .th,.kpi,.sec{break-inside:avoid}.themes{display:block}.th{margin-bottom:8px}
 .sub{background:none}footer{background:none}
}
@media(max-width:900px){.kpis{grid-template-columns:1fr}.split{grid-template-columns:1fr}.mapcol{order:-1}#map{height:320px}}
</style></head><body><div class="wrap">
<p class="sub" id="sub"></p>
<section class="hero"><span class="lab">이번 주 한 줄</span><h2 id="head"></h2></section>
<div class="kpis" id="kpis"></div>
<div class="bar" id="fd"></div><div class="bar" id="ft"></div>
<div class="split"><div class="list" id="list"><div class="cards" id="grid"></div>
<div class="sec" id="thsec">테마별 주의 사항</div><div class="themes" id="themes"></div>
<footer>화제와 상권 구성을 함께 본 참고용 정보이며 투자·창업 권유가 아닙니다. 업종 구성은 소상공인시장진흥공단 상가업소정보의 실측 점포 수입니다. 동(읍·면) 이름과 맞는 동네는 그 동의 점포를, 그 밖(공원·시설 일원 등)은 구·시 전체 점포를 셌으며 카드마다 범위를 적었습니다.</footer></div>
<div class="mapcol" id="mapcol"><h3 class="mapttl">지도 — 선정 동네 위치</h3><div id="map"></div><p class="maplegend">핀의 숫자는 카드 번호, 색은 상권 등급입니다(라임: 양호 · 흰색: 주의 · 검정: 경고). 위치는 동네 부근의 추정 좌표입니다.</p></div></div>
</div><script>
const D=__DATA__;
const $=s=>document.querySelector(s),esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const GL={A:"A · 양호",B:"B · 양호",C:"C · 주의",D:"D · 경고"},gc=g=>({A:"gA",B:"gB",C:"gC",D:"gD"}[g]||"gN"),gl=g=>GL[g]||esc(g||"판단 불가");
$("#sub").textContent=`${D.region?D.region+" · ":""}${D.office} · 관측 ${D.start} ~ ${D.end}`;
$("#head").textContent=D.headline;
// 같은 동네(구+동)의 화제는 카드 하나로 묶는다: 업종 구성·등급은 한 번만
const G=[];{const by=new Map();for(const c of D.cards){const k=c.district+"|"+c.area;let g=by.get(k);if(!g){g={...c,items:[]};by.set(k,g);G.push(g)}g.items.push(c)}}
const themesOf=g=>[...new Set(g.items.map(i=>i.theme))];
// 숫자 타일: 선정 동네 · 테마 · 등급 분포
const nAB=G.filter(g=>g.grade==="A"||g.grade==="B").length,nC=G.filter(g=>g.grade==="C").length,nD=G.filter(g=>g.grade==="D").length,nO=G.length-nAB-nC-nD;
const seg=(n,bg)=>n?`<i style="flex:${n};background:${bg}"></i>`:"";
$("#kpis").innerHTML=`<div class="kpi"><div class="l">선정 동네</div><div class="v">${G.length}곳</div></div>
<div class="kpi"><div class="l">테마</div><div class="v">${new Set(D.cards.map(c=>c.theme)).size}개</div></div>
<div class="kpi"><div class="l">등급 분포</div><div class="dist">${seg(nAB,"#b8f36b")}${seg(nC,"#cfcfcf")}${seg(nD,"#111")}${seg(nO,"#ddd")}</div>
<div class="distl"><span><s style="background:#b8f36b"></s>A·B 양호 ${nAB}</span><span><s style="background:#cfcfcf"></s>C 주의 ${nC}</span><span><s style="background:#111"></s>D 경고 ${nD}</span>${nO?`<span><s style="background:#ddd"></s>판단 불가 ${nO}</span>`:""}</div></div>`;
const st={d:"전체",t:"전체"};
let map=null,layer=null,markers={},tiles=null,lastPts=[];
const pinEl=i=>markers[i]&&markers[i].getElement()?.querySelector(".pin");
function actOn(i,on){const c=document.getElementById("card-"+i);if(c)c.classList.toggle("act",on);const p=pinEl(i);if(p)p.classList.toggle("act",on)}
function focusCard(i){const el=document.getElementById("card-"+i);if(el){el.scrollIntoView({behavior:"smooth",block:"center"});el.classList.add("hl");setTimeout(()=>el.classList.remove("hl"),1600)}}
function drawMap(list){
 if(typeof L==="undefined"){$("#map").innerHTML='<div class="th">지도를 불러오지 못했습니다(인터넷 연결 확인).</div>';return}
 if(!map){map=L.map("map",{scrollWheelZoom:false,maxZoom:13}).setView([35.20,128.63],11);
  tiles=L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",{maxZoom:13,attribution:"Tiles &copy; Esri"}).addTo(map);layer=L.layerGroup().addTo(map)}
 layer.clearLayers();markers={};const pts=[];
 list.forEach(g=>{const i=G.indexOf(g);
  const ic=L.divIcon({className:"",html:`<div class="pin p${g.grade||"N"}">${i+1}</div>`,iconSize:[30,30],iconAnchor:[15,15]});
  markers[i]=L.marker([g.lat,g.lng],{icon:ic,title:g.area}).addTo(layer).bindTooltip(`${i+1}. ${esc(g.area)} (${esc(g.district)})${g.approx?" · 구 중심 표시":""}`)
   .on("click",()=>focusCard(i)).on("mouseover",()=>actOn(i,true)).on("mouseout",()=>actOn(i,false));
  pts.push([g.lat,g.lng])});
 lastPts=pts;setTimeout(()=>{map.invalidateSize();if(pts.length)map.fitBounds(pts,{padding:[40,40],maxZoom:13})},60)}
function chips(el,label,vals,key){$(el).hidden=vals.length<2;if(vals.length<2){$(el).innerHTML="";return}
 $(el).innerHTML=`<b>${label}</b>`+["전체",...vals].map(v=>`<button type="button" class="chip ${st[key]===v?"on":""}" data-v="${esc(v)}">${esc(v)}</button>`).join("");
 $(el).onclick=e=>{const v=e.target.dataset?.v;if(v){st[key]=v;draw()}}}
const COL=["#3563e9","#f08c00","#2f9e44","#e03131","#862e9c","#868e96"];   // 업종 막대는 색으로 구분(핵심 업종이 맨 앞)
function mixBlock(g){
 const mix=g.mix||[];if(!mix.length)return"";
 const fu=g.focus&&g.focus.upjong;let hi=mix.findIndex(m=>fu&&m.name===fu);if(hi<0)hi=0;
 const order=[mix[hi],...mix.filter((_,k)=>k!==hi)];
 const bar=order.map((m,k)=>`<i style="width:${m.share_pct}%;background:${COL[k%6]}" title="${esc(m.name)} ${m.share_pct}%"></i>`).join("");
 const wide=g.focus&&g.focus.upjong===order[0].name&&g.focus.wide_share_pct!=null?g.focus.wide_share_pct:null;
 const lg=order.slice(0,4).map((m,k)=>`<span><s style="background:${COL[k%6]}"></s>${k?"":"<b>"}${esc(m.name)} ${m.share_pct}%${k?"":"</b>"}</span>`).join("");
 return `<div class="mixw${wide!=null?" hasavg":""}">${wide!=null?`<span class="avgl" style="left:${wide}%">시·구 평균 ${wide}%</span>`:""}<div class="mix">${bar}</div>${wide!=null?`<i class="avg" style="left:${wide}%" title="시·구 평균 ${wide}%"></i>`:""}
 <div class="mixl">${lg}<span>· ${g.scope==="동 단위"?esc(g.area)+" 점포":esc(g.district)+" 전체 점포"} ${Number(g.total||0).toLocaleString()}개</span></div></div>`}
function draw(){
 const dset=[...new Set(G.map(g=>g.district))],tset=[...new Set(G.flatMap(themesOf))];
 chips("#fd","구",dset,"d");chips("#ft","테마",tset,"t");
 const list=G.filter(g=>(st.d==="전체"||g.district===st.d)&&(st.t==="전체"||themesOf(g).includes(st.t)));drawMap(list);
 $("#grid").innerHTML=list.map(g=>{
  const i=G.indexOf(g),multi=g.items.length>1;
  const evs=g.items.map((it,k)=>`<div class="ev">${multi?`<span class="evn">화제 ${k+1}</span>`:""}${esc(it.event)}</div>`).join("");
  const advs=[...new Set(g.items.map(it=>it.advice).filter(Boolean))];
  const adv=advs.map((a,k)=>`<div class="adv"><b>소상공인 조언${advs.length>1?" "+(k+1):""}</b>${esc(a)}</div>`).join("");
  const arts=g.items.map(it=>it.key_article||{}).filter(a=>a.url).map(a=>`<a href="${esc(a.url)}" target="_blank" rel="noopener">📰 ${esc(a.domain)} · ${esc(a.published)}</a>`).join("");
  return `<article class="card" id="card-${i}" data-i="${i}"><div class="top"><div class="ttl"><span class="no">${i+1}</span><div><h3>${esc(g.area)}</h3><div class="meta">${esc(g.district)} · ${themesOf(g).map(t=>`<span class="tag">${esc(t)}</span>`).join("")}</div></div></div><span class="grade ${gc(g.grade)}">${gl(g.grade)}</span></div>
  ${evs}${mixBlock(g)}
  ${g.focus&&g.focus.count!=null?`<div class="foc"><b>${esc(g.focus.upjong)}</b> ${Number(g.focus.count).toLocaleString()}개 · ${g.focus.share_pct}%${g.focus.vs_wide?` (시·구 평균 ${g.focus.wide_share_pct}%의 ${g.focus.vs_wide}배)`:""}${(g.focus.detail||[]).length?" · 상위 "+g.focus.detail.slice(0,3).map(d=>esc(d.name)+" "+d.count).join(", "):""}</div>`:""}
  ${adv}
  <details class="more"><summary>근거 보기</summary><p>등급 근거: ${esc(g.grade_reason)}</p>${arts}</details></article>`}).join("")||`<div class="th">조건에 맞는 동네가 없습니다.</div>`;
 document.querySelectorAll(".card").forEach(el=>{const i=+el.dataset.i;
  el.onmouseenter=()=>actOn(i,true);el.onmouseleave=()=>actOn(i,false);
  el.onclick=e=>{if(e.target.closest("a,summary"))return;const g=G[i];if(map&&markers[i]){map.flyTo([g.lat,g.lng],Math.max(map.getZoom(),12),{duration:.5});markers[i].openTooltip()}}});
 fit()}
draw();
// 빈 테마(해당 동네 없음)는 숨긴다
const tl=D.themes.map(t=>({t,mem:G.map((g,i)=>[i,g]).filter(([,g])=>themesOf(g).includes(t.theme))})).filter(x=>x.mem.length);
$("#themes").innerHTML=tl.map(({t,mem})=>`<div class="th"><b>${esc(t.theme)}</b><div class="mem">${mem.map(([i,g])=>`<a data-go="${i}"><i>${i+1}</i>${esc(g.area)}</a>`).join("")}</div>${esc(t.risk||"")}</div>`).join("");
if(!tl.length){$("#thsec").hidden=true}
$("#themes").onclick=e=>{const a=e.target.closest("[data-go]");if(a)focusCard(+a.dataset.go)};
function fit(){if(map)map.invalidateSize()}   // 위쪽 요약은 고정, 왼쪽 카드 목록만 스크롤. 지도는 목록 높이에 맞춘다
addEventListener("resize",fit);
// 인쇄할 때는 접어 둔 "근거 보기"를 모두 펼치고, 인쇄가 끝나면 원래대로 접는다
// 인쇄: 지도를 한 쪽에 꽉 차게 키운 크기(.pp)로 먼저 맞추고 타일이 다 올 때까지 기다린 뒤 인쇄한다(부모 화면의 인쇄 버튼이 부른다)
window.__preparePrint=()=>new Promise(res=>{
 document.documentElement.classList.add("pp");
 document.querySelectorAll("details.more").forEach(d=>{d.dataset.was=d.open?"1":"0";d.open=true});
 if(!map){res();return}
 let fin=false;const done=()=>{if(!fin){fin=true;res()}};
 map.invalidateSize();if(lastPts.length)map.fitBounds(lastPts,{padding:[50,50],maxZoom:13,animate:false});
 if(tiles)tiles.once("load",()=>setTimeout(done,300));
 setTimeout(done,5000)});
addEventListener("beforeprint",()=>{document.querySelectorAll("details.more").forEach(d=>{if(!("was" in d.dataset))d.dataset.was=d.open?"1":"0";d.open=true});document.documentElement.classList.add("pp");if(map){map.invalidateSize();if(lastPts.length)map.fitBounds(lastPts,{padding:[50,50],maxZoom:13,animate:false})}});
addEventListener("afterprint",()=>{document.documentElement.classList.remove("pp");document.querySelectorAll("details.more").forEach(d=>{d.open=d.dataset.was==="1";delete d.dataset.was});fit()});
</script></body></html>"""
