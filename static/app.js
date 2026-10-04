// 경남 소상공인 상권 진단 AI 에이전트 팀 프론트엔드. 해시 라우터로 화면을 오간다:
//   #/agents        경남상권의뢰 상담소
//   #/agents/{id}   직원 소개 (읽기 전용)
//   #/chat/{id}     직원과 대화
//   #/offices       사무실 바로가기 (offices.js)
//   #/offices/{id}  회사 배치      (offices.js)
//   #/offices/{id}/flows/{id}  한 번의 실행 기록 (office_flow.js)
//   #/libraries/{id}  컴퓨터 한 곳 (libraries.js)
//
// 아바타는 DiceBear Voxel Art(CC0)를 vendor/dicebear-voxel.js 로 묶어 로컬에서 그린다.
// 인터넷 연결이 필요 없고, 외부로 아무것도 전송하지 않는다. (묶는 방법: tools/dicebear_build)

const $ = (s, root = document) => root.querySelector(s);
const app = $("#app");

// ---------------------------------------------------------------- 공통

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: opts.body ? { "Content-Type": "application/json" } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    // FastAPI 검증 오류(422)는 detail이 배열이라 사람이 읽을 문장으로 바꾼다
    const detail = Array.isArray(err.detail) ? "입력값을 확인하세요" : err.detail;
    // detail 이 문장이 아니라 객체면(예: 회사 수정의 409 확인 요청) 호출한 쪽이 읽을 수 있게 그대로 붙여 둔다
    const e = new Error(typeof detail === "string" ? detail : detail?.message || `요청 실패 (${res.status})`);
    e.status = res.status;
    e.detail = detail;
    throw e;
  }
  return res.json();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------------------------------------------------------------- 아바타

const DEFAULT_LOOK = {
  seed: "agent-town", top: "short", outfit: "plain", eyes: "open", mouth: "smile", glasses: "", beard: "",
  skin_color: "#f5d0b0", hair_color: "#2c222b", shirt_color: "#228be6", background_color: "#d1d4f9",
};

// 저장된 외모에 빠진 값이 있거나(예전 형식) 그림을 못 그리는 값이 있어도 화면이 깨지지 않게 기본 모습으로 대체한다.
function avatarSrc(look, opts = {}) {
  try {
    return VoxelAvatar.dataUri({ ...DEFAULT_LOOK, ...look }, opts);
  } catch {
    return VoxelAvatar.dataUri(DEFAULT_LOOK, opts);
  }
}

// opts.transparent: 배경 없이 캐릭터만 (장면 위에 얹을 때)
function avatarImg(look, size, opts = {}) {
  const img = document.createElement("img");
  img.className = "avatar";
  img.alt = "";
  img.width = img.height = size;
  img.src = avatarSrc(look, opts);
  return img;
}


// ---------------------------------------------------------------- 라우터

// 대시보드 — 지난 보고서를 드롭다운에서 골라 열어 본다(서버가 조립한 HTML 을 그대로 끼운다). 기본은 가장 최근 보고서.
// 시안 v3 열람실 첫 화면: 완성된 보고서를 영화 포스터처럼 전시한다. 포스터를 누르면 그 보고서가 열리고, 작성 중인 보고서는 "준비 중" 포스터로 보인다.
let galleryTimer = null;
async function renderGallery(office, reg, reports, newestId) {
  clearTimeout(galleryTimer);
  app.classList.add("wide");
  const stamp = (iso) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? "" : `${d.getMonth() + 1}월 ${d.getDate()}일`; };
  const pos = ["12% 40%", "58% 30%", "88% 55%", "30% 85%", "70% 78%", "45% 12%", "5% 70%", "95% 20%"];
  const gradeBar = (g) => { const n = (k) => (g && g[k]) || 0; const ab = n("A") + n("B"), c = n("C"), d = n("D"), t = ab + c + d || 1; return `<i style="flex:${ab};background:#b8f36b"></i><i style="flex:${c};background:#fff"></i><i style="flex:${d};background:#111"></i>`.replace(/flex:0;/g, "flex:0;") + (t ? "" : ""); };
  const RIMG = { 창원: "changwon", 진주: "jinju", 김해: "gimhae", 양산: "yangsan", 거제: "geoje" };   // 지역 상징 그림(static/region/)
  const imgOf = (name) => { const k = String(name || "").replace(/시$/, ""); return RIMG[k] ? `url('region/${RIMG[k]}.jpg')` : "url('bg_archipelago.jpg')"; };
  const cards = reports.map((r, i) => {
    const g = r.grades || {}; const ab = (g.A || 0) + (g.B || 0);
    return `<a class="poster${r.flow_id === newestId ? " fresh" : ""}" href="#/dashboard/${esc(r.flow_id)}" style="--bgpos:center;--img:${imgOf(r.region)}">
      <span class="po-art"><em>${esc(stamp(r.finished_at))}</em>${r.flow_id === newestId ? '<b class="po-new">방금 완성</b>' : ""}</span>
      <span class="po-body"><strong>${esc(r.region || "경남")}</strong><small>${esc(r.scope || "")}</small>
        <span class="po-head">${r.headline ? `“${esc(r.headline)}”` : ""}</span>
        <span class="po-bar">${gradeBar(g)}</span>
        <span class="po-meta">동네 ${r.areas ?? "-"}곳${r.areas ? ` · 양호 ${ab}` : ""} <b class="po-read">읽어 보기 →</b></span></span></a>`;
  });
  // 작성 중인 의뢰: 준비 중 포스터
  let runCard = "";
  try {
    const cur = (await api("/api/regions")).running;
    if (cur) {
      const f = await api(`/api/offices/${office.id}/flows/${cur}`);
      const inp = f.inputs || {}, label = [inp.region, inp.area, inp.upjong].filter(Boolean).join(" · ");
      runCard = `<a class="poster preparing" href="#/offices/${esc(office.id)}"><span class="po-art"><span class="po-spin"></span></span>
        <span class="po-body"><strong>준비 중</strong><small>${esc(label)}</small><span class="po-bar run"><i></i></span><span class="po-meta">보고서를 작성하고 있어요</span><span class="po-head">${esc(f.stage || "")}</span></span></a>`;
      galleryTimer = setTimeout(() => { if (location.hash === "#/dashboard") renderGallery(office, reg, reports, newestId); }, 4000);
    }
  } catch { /* 준비 중 표시는 없어도 된다 */ }
  const lead = reports.find((r) => r.headline);
  app.innerHTML = `<div class="dash-bar dash-head"><div><h1>보고서 열람실</h1><p class="dash-sub">완성된 상권 진단 보고서를 전시해 둔 곳 · 포스터를 눌러 펼쳐 보세요</p></div></div>
    ${lead ? `<a class="lead" href="#/dashboard/${esc(lead.flow_id)}"><span class="lead-k">이번 주 읽을 만한 한 줄</span><span class="lead-h">${esc(lead.headline)}</span><span class="lead-m">${esc(lead.region || "")} · ${esc(lead.scope || "")} · 읽어 보기 →</span></a>` : ""}
    <div class="gallery">${runCard}${cards.join("")}${!runCard && !cards.length ? `<div class="empty">아직 만들어진 보고서가 없어요.<br><a href="#/agents">상담소에서 지역을 골라 조사를 의뢰해 보세요.</a></div>` : ""}</div>`;
}

async function renderDashboard(onlyFlow) {
  const [offices, reg] = await Promise.all([api("/api/offices"), api("/api/regions")]);
  const office = offices.find((o) => o.id === reg.office_id) || offices[0];
  if (!office) { app.innerHTML = `<div class="empty">사무실이 없습니다.</div>`; return; }
  const reports = (await api(`/api/offices/${office.id}/reports`)).reports || [];
  const v3 = document.documentElement.hasAttribute("data-v3");
  if (v3 && !onlyFlow) {   // 열람실에 들어오면 먼저 전시를 보여 준다
    let fresh = null;
    try { fresh = localStorage.getItem("dashFlow"); localStorage.removeItem("dashFlow"); } catch { /* 무시 */ }
    try {
      const fl = ((await api(`/api/offices/${office.id}/flows`)).flows || []).find((x) => x.status !== "running");
      localStorage.setItem("ackFlow", (fl && fl.id) || (reports[0] && reports[0].flow_id) || "");   // 열람실에 오면 상담소는 다시 의뢰할 수 있는 상태로 초기화
    } catch { /* 초기화하지 못해도 열람실은 열린다 */ }
    await renderGallery(office, reg, reports, fresh);
    return;
  }
  if (!reports.length) {
    app.innerHTML = `<div class="empty">아직 만들어진 보고서가 없어요.<br><a href="#/agents">상담소에서 지역을 골라 조사를 의뢰해 보세요.</a></div>`;
    return;
  }
  let savedRegion = null, savedFlow = onlyFlow || null;
  try { savedRegion = localStorage.getItem("dashRegion"); if (!onlyFlow) savedFlow = localStorage.getItem("dashFlow"); localStorage.removeItem("dashFlow"); } catch { /* 저장된 값이 없어도 된다 */ }
  // 상담소에서 '보고서 확인하기'로 왔으면 그 보고서, 지역 링크로 왔으면 그 지역의 가장 최근 보고서, 아니면 가장 최근 보고서
  clearTimeout(galleryTimer);
  let cur = (reports.find((r) => r.flow_id === savedFlow) || reports.find((r) => r.region === savedRegion) || reports[0]).flow_id;
  // 열람실에 들어오면 지금까지의 의뢰 결과를 확인한 것으로 보고, 다른 화면(상담소)은 다시 의뢰할 수 있는 상태로 초기화한다
  try {
    const fl = ((await api(`/api/offices/${office.id}/flows`)).flows || []).find((x) => x.status !== "running");
    localStorage.setItem("ackFlow", (fl && fl.id) || reports[0].flow_id);
  } catch { /* 초기화하지 못해도 열람실은 열린다 */ }
  app.classList.add("wide");
  app.innerHTML = `<div class="dash-bar${v3 ? " dash-head" : ""}">${v3 ? "<div><a class=\"dash-back\" href=\"#/dashboard\">← 열람실 전시로</a><h1>보고서 열람실</h1><p class=\"dash-sub\">완성된 상권 진단 보고서를 펼쳐 보는 곳</p></div>" : ""}<span class="dash-tools"><label class="dash-pick" for="dashRun"><span>보고서</span>
      <select id="dashRun" aria-label="지난 보고서 선택"></select></label>${v3 ? '<button type="button" id="dashPrint" class="primary" title="이 보고서를 인쇄하거나 PDF 파일로 저장합니다">인쇄 / PDF 저장</button>' : ""}</span></div>
    <iframe id="dashFrame" title="상권 진단 보고서 대시보드" style="width:100%;height:${v3 ? "max(520px, calc(100vh - 256px))" : "calc(100vh - 118px)"};border:0;display:block;background:transparent"></iframe>`;
  const stamp = (iso) => {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? "" : `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  };
  $("#dashRun").innerHTML = reports.map((r, i) => `<option value="${esc(r.flow_id)}">${esc(stamp(r.finished_at))} · ${esc(r.scope)}${i === 0 ? " (최신)" : ""}</option>`).join("");
  const show = () => {
    $("#dashRun").value = cur;
    const r = reports.find((x) => x.flow_id === cur);
    try { if (r) localStorage.setItem("dashRegion", r.region); } catch { /* 무시 */ }
    $("#dashFrame").src = `/api/offices/${office.id}/dashboard?flow_id=${encodeURIComponent(cur)}${v3 ? "&v=v3" : ""}`;
  };
  $("#dashRun").addEventListener("change", (e) => { cur = e.target.value; show(); });
  $("#dashPrint")?.addEventListener("click", async () => {   // 인쇄 창에서 "PDF로 저장"을 고르면 파일이 된다. 파일 이름 = 문서 제목
    const w = $("#dashFrame")?.contentWindow;
    if (!w) return;
    const r = reports.find((x) => x.flow_id === cur);
    try { w.document.title = `상권진단보고서_${r ? r.scope.replace(/\s*·\s*/g, "_").replace(/\s+/g, "") : ""}_${(r?.finished_at || "").slice(0, 10)}`; } catch { /* 제목은 그대로 */ }
    w.focus();
    const btn = $("#dashPrint");
    if (btn) { btn.disabled = true; btn.textContent = "지도 준비 중…"; }
    try { await w.__preparePrint?.(); } catch { /* 준비에 실패해도 인쇄는 한다 */ }
    if (btn) { btn.disabled = false; btn.textContent = "인쇄 / PDF 저장"; }
    w.print();
  });
  show();
}

const routes = [
  [/^#\/dashboard$/, () => renderDashboard(), "dashboard"],
  [/^#\/dashboard\/(f_[0-9a-f]+)$/, (m) => renderDashboard(m[1]), "dashboard"],
  [/^#\/agents\/(a_[0-9a-f]{8})$/, (m) => renderAgentProfilePage(m[1]), "agents"],   // 직원 소개(읽기 전용)
  [/^#\/agents\/(a_[0-9a-f]{8})\/edit$/, (m) => { location.replace(`#/agents/${m[1]}`); }, "agents"],   // 옛 주소는 소개로
  [/^#\/agents$/, () => renderList(), "agents"],
  [/^#\/offices$/, () => renderOfficeList(), "offices"],
  [/^#\/offices\/(o_[0-9a-f]{8})$/, (m) => renderOfficeDetail(m[1]), "offices"],
  [/^#\/offices\/(o_[0-9a-f]{8})\/flows\/(f_[0-9a-f]{8})$/, (m) => renderFlowRun(m[1], m[2]), "offices"],
  [/^#\/libraries$/, () => { location.replace("#/offices"); }, "offices"],   // 컴퓨터 목록 화면은 없앴다(옛 주소는 사무실로)
  [/^#\/libraries\/(l_[0-9a-f]{8})$/, (m) => renderLibraryDetail(m[1]), "libraries"],
  [/^#\/chat\/(a_[0-9a-f]{8})$/, (m) => renderChat(m[1]), "agents"],
];

async function route() {
  app.classList.remove("wide");   // 넓은 화면(회사 배치)은 그 화면이 스스로 켠다
  const hash = location.hash || "#/agents";
  const match = routes.map(([re, fn, nav]) => [re.exec(hash), fn, nav]).find(([m]) => m);
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === match?.[2]));
  if (!match) { location.hash = "#/agents"; return; }
  try {
    await match[1](match[0]);
  } catch (err) {
    app.innerHTML = `<div class="empty">${esc(err.message)}<br><a href="#/agents">목록으로</a></div>`;
  }
}
window.addEventListener("hashchange", route);

// ---------------------------------------------------------------- 직원 소개(읽기 전용)

// 직원의 소개글(지시문)을 보기 좋은 글로: # 제목, ## 소제목, - 목록, **굵게** 만 처리한다.
function profileText(src) {
  const inline = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`(.+?)`/g, "<code>$1</code>");
  const out = []; let list = null;
  const flush = () => { if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; } };
  for (const raw of String(src || "").split("\n")) {
    const line = raw.trimEnd();
    let m;
    if ((m = /^(#{1,3})\s+(.*)$/.exec(line))) { flush(); out.push(`<h${m[1].length + 2}>${inline(m[2])}</h${m[1].length + 2}>`); }
    else if ((m = /^\s*(?:[-*]|\d+\.)\s+(.*)$/.exec(line))) (list ||= []).push(`<li>${inline(m[1])}</li>`);
    else if (!line.trim()) flush();
    else { flush(); out.push(`<p>${inline(line)}</p>`); }
  }
  flush();
  return out.join("");
}

// 직원 한 명을 소개하는 화면. 고치거나 저장하지 않고 보여 주기만 한다(탭: 소개·모델·전문자료·지식 그래프·도구).
function renderAgentProfile(agent) {
  app.innerHTML = `
    <div class="page-head">
      <a class="back" href="#/agents">← 경남상권의뢰 상담소</a>
      <h1>직원 소개</h1>
      <p>AI 직원의 성격과 일하는 방식, 쓰는 도구를 소개합니다.</p>
    </div>
    <div class="create profile-mode">
      <div class="form">
        <div class="subtabs" role="tablist">
          <button type="button" role="tab" class="subtab on" data-tab="persona" aria-selected="true">소개</button>
          <button type="button" role="tab" class="subtab" data-tab="model" aria-selected="false">모델</button>
          <button type="button" role="tab" class="subtab" data-tab="docs" aria-selected="false">전문자료</button>
          <button type="button" role="tab" class="subtab" data-tab="graph" aria-selected="false">지식 그래프</button>
          <button type="button" role="tab" class="subtab" data-tab="tools" aria-selected="false">도구</button>
        </div>
        <section class="tab-panel" data-panel="persona" role="tabpanel">
          <div class="pf-head"><h2>${esc(staffName(agent))}</h2><span>${esc(agent.role)}</span></div>
          ${agent.tagline ? `<p class="pf-tag">${esc(agent.tagline)}</p>` : ""}
          <div class="pf-body">${profileText(agent.persona)}</div>
        </section>
        <section class="tab-panel" data-panel="model" role="tabpanel" hidden><div id="modelRoot"></div></section>
        <section class="tab-panel" data-panel="docs" role="tabpanel" hidden><p class="pf-note">전문자료는 직원이 일할 때 <b>참고서처럼 옆에 두고 찾아보는 문서</b>입니다. 화제를 판별하는 기준 가이드와 창원·진주·김해·양산·거제의 행정구역 표가 들어 있어서, 직원이 동네 이름을 읽을 때 이 서류를 근거로 판단합니다. 아래는 지금 들어 있는 서류 목록입니다.</p><div id="docsRoot"></div></section>
        <section class="tab-panel" data-panel="graph" role="tabpanel" hidden><p class="pf-note" id="graphNote"></p><div id="graphRoot"></div></section>
        <section class="tab-panel" data-panel="tools" role="tabpanel" hidden><p class="pf-note">도구는 직원이 일하면서 <b>꺼내 쓰는 기능</b>입니다. 기사를 찾아 읽고, 참고 서류를 검색하고, 공공 상가 데이터로 숫자를 세는 일을 도구가 대신해 줍니다. 아래는 이 직원이 쓰는 도구만 모아 둔 것입니다.</p><div id="toolsRoot"></div></section>
      </div>
      <aside class="preview">
        <div id="pvSlot"></div>
        <div class="pv-name">${esc(staffName(agent))}</div>
        <div class="muted">${esc(agent.role)}</div>
        <div id="pvModel" class="pv-meta"></div>
        <div id="pvTasks" class="pv-meta"></div>
      </aside>
    </div>`;
  const root = app;
  $("#pvSlot").replaceChildren(avatarImg(agent.appearance, 168));
  const modelPicker = createModelPicker($("#modelRoot"), () => { $("#mdSaveMsg").textContent = ""; $("#mdSave").disabled = false; }, { initial: agent.model, originalProvider: agent.model.provider });
  // 모델 탭만 바꿀 수 있다: 공급사·모델을 고르고 저장하면 그 직원이 다음 일부터 그 모델을 쓴다(키가 없는 공급사는 서버가 거절한다)
  $("#modelRoot").insertAdjacentHTML("beforeend", `<div class="md-save"><button type="button" class="primary" id="mdSave" disabled>이 모델로 바꾸기</button><span id="mdSaveMsg" class="muted small"></span></div>`);
  $("#mdSave").addEventListener("click", async () => {
    const msg = $("#mdSaveMsg"), btn = $("#mdSave");
    const problem = modelPicker.validate();
    if (problem) { msg.textContent = problem; return; }
    btn.disabled = true; msg.textContent = "저장하는 중…";
    try {
      const r = await api(`/api/agents/${agent.id}/model`, { method: "PUT", body: modelPicker.getValue() });
      agent.model = r.model;
      $("#pvModel").textContent = modelTag(r.model);
      msg.textContent = "바꿨습니다. 다음 일부터 이 모델을 씁니다.";
    } catch (err) { msg.textContent = err.message; btn.disabled = false; }
  });
  createDocsPanel($("#docsRoot"), { agentId: agent.id, providerName: () => PROVIDER_SHORT[modelPicker.getValue().provider] ?? "" });
  createGraphPanel($("#graphRoot"), { agentId: agent.id });
  // 지식 그래프 탭 설명: 직원마다 그래프에 들어 있는 내용이 달라서 직원별로 쓴다
  const GRAPH_NOTE = {
    a_7612ec55: "이 지도에는 <b>창원·진주·김해·양산·거제의 시·구·동 이름과 소속 관계</b>(예: 창원시 › 성산구 › 상남동)가 들어 있습니다. 기사에 동네 이름이 나오면 <b>어느 구의 어느 동네인지</b> 바로 확인해서, 같은 이름의 다른 동네와 헷갈리지 않고 화제 동네를 고릅니다.",
    a_bcb2ef08: "이 지도에는 <b>상권 테마의 종류와 대표 사례</b>가 들어 있습니다. 신규 개발·재개발, 축제·계절 상권, 대형시설 개장, 교통망 변화, 임대료·공실 확산, 업종 과열이라는 여섯 갈래와 각각의 예(진해 군항제, 진주 남강유등축제 등)를 보고, 화제 동네들이 <b>어떤 테마로 묶이는지</b> 판단합니다.",
    a_a28223e1: "이 지도에는 <b>상권 등급(A 기회 · B 평범 · C 과열 주의 · D 경고)과 업종 구성 신호</b>가 들어 있습니다. 특정 업종이 40% 이상이면 경쟁 과열, 상위 업종이 20% 미만이면 안정적이라는 식의 기준을 보고, 점포 수를 센 결과를 <b>어느 등급으로 매길지</b> 판단합니다.",
    a_b5f24ab2: "이 지도에는 <b>보고서에 넣을 동네를 고르는 원칙</b>이 들어 있습니다. 구당 최대 2곳, 사건이 뚜렷한 곳 우선, 단정적인 추천 금지 같은 규칙을 보고, 앞 직원들의 결과에서 <b>어떤 동네를 골라 어떻게 쓸지</b> 정합니다.",
  };
  $("#graphNote").innerHTML = GRAPH_NOTE[agent.id] || "지식 그래프는 직원이 읽은 자료에서 이름과 그 관계를 뽑아 정리한 지도입니다.";
  // 도구 탭: 이 직원이 쓰는 도구만 한 줄 소개로 보여 준다(고르거나 시험하는 화면은 없다)
  api("/api/tools").then((d) => {
    const all = Array.isArray(d) ? d : d.tools || [];
    const mine = (agent.tools || []).map((id) => all.find((t) => t.id === id)).filter(Boolean);
    const brief = (t) => { const first = String(t.description || "").split(/(?<=[.다요])\s/)[0]; return first.length > 90 ? first.slice(0, 90) + "…" : first; };
    $("#toolsRoot").innerHTML = mine.length
      ? `<div class="tool-brief">${mine.map((t) => `<div class="tb-item"><b>${esc(t.label || t.id)}</b><code>${esc(t.id)}</code><p>${esc(brief(t))}</p></div>`).join("")}</div>`
      : `<div class="empty">이 직원은 따로 쓰는 도구가 없습니다.</div>`;
  }).catch(() => { $("#toolsRoot").innerHTML = ""; });
  $("#pvModel").textContent = modelTag(modelPicker.getValue());
  const n = (agent.work?.tasks || []).length;
  $("#pvTasks").textContent = n ? `정해진 업무 ${n}개` : "";

  // 다른 탭의 칸은 보여 주기만 한다: 입력은 잠그고 고치는 버튼은 숨긴다(늦게 그려지는 칸도 같은 방식으로)
  const MUT = /삭제|수정|시험 호출|올리기|업로드|추가|저장|다시 색인|색인 실행|파일에서|채우기|새로|되돌리|취소|연결 끊|분리|적용|초기화|반영하기|그래프 만들기|중단|^×$/;
  const lock = () => {
    root.querySelectorAll(".create input, .create textarea, .create select").forEach((el) => { if (!el.closest(".pf-body") && !el.closest("#modelRoot")) { if (["checkbox", "radio"].includes(el.type) || el.tagName === "SELECT") el.disabled = true; else el.readOnly = true; } });
    root.querySelectorAll(".create button:not(.subtab)").forEach((btn) => { if (!btn.closest("#modelRoot") && MUT.test(btn.textContent || "")) btn.hidden = true; });
  };
  lock();
  new MutationObserver(lock).observe(root.querySelector(".create"), { childList: true, subtree: true });

  const showTab = (name) => {
    root.querySelectorAll(".subtab").forEach((b) => { const on = b.dataset.tab === name; b.classList.toggle("on", on); b.setAttribute("aria-selected", on); });
    root.querySelectorAll(".tab-panel").forEach((panel) => { panel.hidden = panel.dataset.panel !== name; });
    if (name === "graph") $("#graphRoot").dispatchEvent(new Event("graph-shown"));
    if (name === "tools") $("#toolsRoot").dispatchEvent(new Event("tools-shown"));
  };
  root.querySelector(".subtabs").addEventListener("click", (e) => { const t = e.target.closest(".subtab"); if (t) showTab(t.dataset.tab); });
}

// 직원 소개 — 읽기 전용이다(고치는 기능은 없앴다). 주소: #/agents/{id}
async function renderAgentProfilePage(agentId) {
  renderAgentProfile(await api(`/api/agents/${agentId}`));
}

// ---------------------------------------------------------------- 경남상권의뢰 상담소

async function renderList() {
  const [offices, agents, reg] = await Promise.all([api("/api/offices"), api("/api/agents"), api("/api/regions")]);
  const office = offices.find((o) => o.id === reg.office_id) || offices[0];
  if (!office) { app.innerHTML = `<div class="empty">사무실이 없습니다.</div>`; return; }
  app.classList.add("wide");
  const byAgent = new Map(agents.map((a) => [a.id, a]));
  // 부서 순서(= 일하는 순서)대로 직원
  const staff = office.departments.flatMap((d) => Array.from({ length: d.desks }, (_, i) => {
    const a = byAgent.get(office.assignments[`${d.id}:${i}`]);
    return a ? { a, dept: d.name } : null;
  })).filter(Boolean);

  app.innerHTML = `
    <div class="page-head"><h1>경남상권의뢰 상담소</h1>
      <p>궁금한 지역을 골라 주세요. 저희 AI 직원 4명이 이번 주 화제가 된 동네를 찾아 상권을 함께 살펴드려요.</p></div>
    <section class="team-block" aria-label="상담소 직원과 진행 상황">
      <div class="staff" id="staff"></div>
      <div id="commissionState"></div>
    </section>
    <div class="cm2" id="cmRoot">
      <div id="cmTop"></div>
      <div class="cm-body" id="cmBody">
        <aside class="cm-side" id="cmSide" aria-label="의뢰 조건" hidden></aside>
        <div class="cm-map"><div id="regionMap"></div><div class="muted small cm-legend"><i class="cm-dot"></i>● 회색 원은 화제 감지 지점 · 클수록 점포가 많은 곳</div></div>
      </div>
    </div>`;

  if (document.documentElement.hasAttribute("data-v3")) {   // 시안 v3: 머리글 문구
    const ph = $(".page-head");
    if (ph) ph.innerHTML = `<span class="hero-kick"><i></i>이번 주 상권 리포트</span>
      <h1><span class="h-l1">이번 주 우리 동네 상권,</span><span class="h-l2"><em>한눈에</em> 진단합니다.</span></h1>
      <p class="hero-sub">AI 직원 4명이 <span class="hk"><i>1</i>화제 탐지</span><span class="hk"><i>2</i>테마 연결</span><span class="hk"><i>3</i>상권 분석</span><span class="hk"><i>4</i>리포트</span> 순서로 한 번에 해드려요.</p>`;
  }

  // 직원(보여 주기만 한다)
  const box = $("#staff");
  staff.forEach(({ a, dept }, i) => {
    if (i) box.insertAdjacentHTML("beforeend", `<span class="staff-arrow" aria-hidden="true">→</span>`);
    const el = document.createElement("div");
    el.className = "staff-card";
    el.dataset.agent = a.id;
    el.innerHTML = `<div class="bubble" role="status" aria-live="polite" hidden></div><div class="slot"></div><div><b>${esc(staffName(a))}</b><div class="muted small">${esc(dept)} · ${esc(a.role)}</div><div class="small">${esc(({ "김쫑긋 사원": "지역 화제 탐지", "박이음 대리": "상권 테마 연결", "오꼼꼼 과장": "실제 데이터 분석", "정또박 팀장": "주간 리포트 작성" })[staffName(a)] || a.tagline || "")}</div></div>`;
    $(".slot", el).replaceWith(avatarImg(a.appearance, 64));
    box.appendChild(el);
  });

  const fmt = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const n = (v) => Number(v).toLocaleString();
  const trees = {};              // 지역 id → 구역·업종 트리(공공 상가 데이터)
  let sel = null;                // 지금 펼친 지역과 고른 값 { r, tree, district, dong, large, mid, days }
  let running = reg.running;     // 지금 실행 중인 흐름 id
  let runRegion = "";            // 지금 의뢰 중인 지역 이름(시·군 타일에 '의뢰 중'을 표시한다)
  let runLabel = "", runStage = "";   // 의뢰 중 표시에 쓰는 조건 글과 지금 단계
  let doneInfo = null;                // 끝났지만 아직 확인하지 않은 의뢰(시안 v3: 의뢰하기 버튼 자리에 알림을 보인다)
  const span = () => {
    const end = new Date(), start = new Date(end.getTime() - (sel?.days || 14) * 86400000);
    return { start: fmt(start), end: fmt(end) };
  };
  const areaText = () => (!sel || !sel.dong ? sel?.district || "" : (sel.tree.multi_district ? `${sel.district} ${sel.dong}` : sel.dong));
  const upText = () => sel?.mid || sel?.large || "";

  const chip = (attr, val, label, cnt, on, thin) => `<button type="button" class="chip${on ? " on" : ""}${thin ? " thin" : ""}" ${attr}="${esc(val)}"${running ? " disabled" : ""}${thin ? ' title="점포가 150개 미만이라 자료가 적어 결과가 약할 수 있습니다"' : ""}>${esc(label)}${cnt != null ? ` <small>${n(cnt)}</small>` : ""}</button>`;

  function panel() {
    const { tree } = sel;
    const dObj = tree.districts.find((x) => x.name === sel.district);
    const dongs = tree.multi_district ? (dObj ? dObj.areas : []) : tree.districts[0].areas;
    const large = tree.upjong.find((u) => u.name === sel.large);
    const s = span();
    const thin = sel.dong && dongs.find((a) => a.name === sel.dong)?.thin;
    const lt = sel.r.latest;
    return `<div class="rgn-v3head"><div class="rv-k">선택한 지역 · ${sel.r.districts.length > 1 ? `구 ${sel.r.districts.length}곳` : `동·읍·면 ${sel.r.areas}곳`}</div><h2>${esc(sel.r.name)}</h2>
      ${lt ? `<p>최근 리포트 · ${esc(lt.scope || sel.r.name)} (${esc((lt.finished_at || "").slice(5, 10).replace("-", "/"))})</p><a class="rv-btn" href="#/dashboard" data-dash="${esc(sel.r.name)}">이번 주 리포트 보기 →</a>` : `<p>아직 리포트가 없어요. 아래에서 조건을 고르고 의뢰해 보세요.</p>`}</div>
    <div class="rgn-panel">
      <div class="step"><span class="step-n">1</span><div class="step-b"><div class="step-t">구역</div>
        <div class="chips">${chip("data-city", "1", "시 전체", null, !sel.district && !sel.dong)}${tree.multi_district ? tree.districts.map((d) => chip("data-d", d.name, d.name, d.count, sel.district === d.name)).join("") : ""}</div>
        ${dongs.length && (sel.district || !tree.multi_district) ? `<div class="chips chips-sub">${dongs.map((a) => chip("data-a", a.name, a.name, a.count, sel.dong === a.name, a.thin)).join("")}</div>` : ""}</div></div>
      <div class="step"><span class="step-n">2</span><div class="step-b"><div class="step-t">업종 <small class="muted">선택</small></div>
        <div class="scope-sel"><select id="upLarge" aria-label="업종 대분류"${running ? " disabled" : ""}><option value="">업종 전체</option>${tree.upjong.map((u) => `<option value="${esc(u.name)}"${u.name === sel.large ? " selected" : ""}>${esc(u.name)} (${n(u.count)})</option>`).join("")}</select>
        ${large ? `<select id="upMid" aria-label="업종 중분류"${running ? " disabled" : ""}><option value="">${esc(large.name)} 전체</option>${large.middle.map((m) => `<option value="${esc(m.name)}"${m.name === sel.mid ? " selected" : ""}>${esc(m.name)} (${n(m.count)})</option>`).join("")}</select>` : ""}</div></div></div>
      <div class="step"><span class="step-n">3</span><div class="step-b"><div class="step-t">관측 기간</div>
        <div class="seg">${[[7, "1주"], [14, "2주"], [30, "30일"]].map(([d, l]) => `<button type="button" class="${sel.days === d ? "on" : ""}" data-days="${d}"${running ? " disabled" : ""}>${l}</button>`).join("")}<span class="muted small">${esc(s.start)} ~ ${esc(s.end)}</span></div></div></div>
      ${running ? `<div class="run-big" id="runBig"><div class="rb-t">의뢰 중<span class="rb-dots"><i></i><i></i><i></i></span></div><div class="rb-s">${esc(runLabel)}</div><div class="rb-st">${esc(runStage)}</div><a class="rb-go" href="#/offices/${esc(office.id)}">사무실에서 일하는 모습 보러 가기 →</a></div>` : ""}
      <div class="rgn-sum"><b>${esc([sel.r.name, areaText(), upText()].filter(Boolean).join(" · "))}</b>
        ${thin ? `<div class="small warn">이 동네는 점포가 적어 자료가 부족할 수 있습니다.</div>` : `<div class="small muted">직원 4명이 조사를 시작합니다(모델 호출 비용이 듭니다).</div>`}</div>
      ${doneInfo && !running ? `<div class="done-box ${esc(doneInfo.status)}"><div class="db-t">${doneInfo.status === "ok" ? `${esc(doneInfo.label)} 보고서를 작성했어요 ✓` : doneInfo.status === "stopped" ? `${esc(doneInfo.label)} 조사가 중단됐어요` : `${esc(doneInfo.label)} 조사에 문제가 생겼어요`}</div>
        <div class="db-b">${doneInfo.status === "ok" ? `<button type="button" class="cta-ok" data-done="open">보고서 확인하기</button>` : `<a class="btn" href="#/offices/${esc(office.id)}/flows/${esc(doneInfo.flowId)}">실행 기록 보기</a>`}</div></div>` : ""}
      <button type="button" class="cta" id="goBtn"${running ? " disabled" : ""}>${running ? "의뢰 진행 중…" : "이 조건으로 의뢰하기"}</button>
    </div>`;
  }

  const side = $("#cmSide");
  // 경로(경상남도 › 시·군 › 구 › 동): 각 단계를 누르면 그 단계로 돌아간다. 선택한 시·군만 펼쳐서 공간을 아낀다.
  const crumbs = () => {
    const cur = (on) => (on ? " cur" : "");
    const parts = [`<button type="button" class="crumb${cur(!sel)}" data-crumb="root">경상남도</button>`];
    if (sel) {
      const multi = sel.tree.multi_district;
      parts.push(`<button type="button" class="crumb${cur(!sel.district && !sel.dong)}" data-crumb="city">${esc(sel.r.name)}</button>`);
      if (multi && sel.district) parts.push(`<button type="button" class="crumb${cur(!sel.dong)}" data-crumb="district">${esc(sel.district)}</button>`);
      if (sel.dong) parts.push(`<span class="crumb cur">${esc(sel.dong)}</span>`);
    }
    return `<nav class="crumbs" aria-label="의뢰 지역 경로">${parts.join('<span class="crumb-sep" aria-hidden="true">›</span>')}</nav>`;
  };
  const resultLink = (r) => (r.latest
    ? `<a class="rgn-res" href="#/dashboard" data-dash="${esc(r.name)}" title="최근 결과 ${esc((r.latest.finished_at || "").slice(0, 10))}">대시보드 · ${esc((r.latest.finished_at || "").slice(5, 10).replace("-", "/"))}</a>`
    : `<span class="rgn-res none">결과 없음</span>`);
  function drawSide() {
    const hint = running ? "지금 조사가 진행 중이에요. 끝나거나 중지한 뒤에 다른 지역을 골라 주세요." : sel ? "" : "어느 지역이 궁금하세요? 시·군을 골라 주세요.";
    $("#cmTop").innerHTML = crumbs() + (hint ? `<p class="cm-hint muted small">${hint}</p>` : "") + `<div class="rgn-row" role="group" aria-label="시·군 선택">` + reg.regions.map((r) => {
      const isRun = running && r.name === runRegion;
      const held = !!(running || doneInfo);   // 의뢰 중이거나, 끝났어도 "보고서 확인하기"를 누르기 전에는 지역을 바꿀 수 없다
      const cls = `${sel && sel.r.id === r.id ? " on" : ""}${isRun ? " running" : held ? " locked" : ""}`;
      return `<div class="rgn-chip${cls}"><button type="button" class="rgn-btn" data-pick="${esc(r.id)}"${held ? " disabled" : ""}${running && !isRun ? ' title="다른 지역을 의뢰하는 중입니다"' : ""}>
        ${r.latest ? '<i class="rgn-dot" title="리포트 있음" aria-hidden="true"></i>' : ""}<span class="rgn-name">${esc(r.name)}</span>${r.roman ? `<span class="rgn-roman">${esc(r.roman)}</span>` : ""}<span class="rgn-sub">${r.districts.length > 1 ? `구 ${r.districts.length}개` : `동·읍·면 ${r.areas}곳`}</span></button>
        ${isRun ? `<span class="rgn-run"><i class="rgn-spin" aria-hidden="true"></i>의뢰 중</span>` : ""}</div>`;
    }).join("") + `</div>`;
    $("#cmBody").classList.toggle("has-panel", !!sel);
    side.hidden = !sel;
    side.innerHTML = sel ? `<section class="rgn on">${panel()}</section>` : "";
    markPins();
    syncMap();
    if (map) setTimeout(() => map.invalidateSize(), 60);   // 지도 칸 크기가 바뀌었으니 다시 맞춘다
  }

  // 지도: 지역마다 핀 하나. 핀을 누르는 것은 왼쪽 목록에서 그 지역을 누르는 것과 같다.
  const pins = {};
  let map = null, dongLayer = null, allPts = [];
  const markPins = () => {
    for (const [id, m] of Object.entries(pins)) {
      m._icon?.querySelector(".rpin")?.classList.toggle("sel", !!sel && sel.r.id === id);
      m.setOpacity(sel ? 0 : 1);   // 시·군을 고른 뒤에는 그 안의 동네 점이 보이도록 시·군 핀은 숨긴다
      if (m._icon) m._icon.style.pointerEvents = sel ? "none" : "";
    }
  };
  const mapEl = $("#regionMap");
  if (typeof L === "undefined") {
    mapEl.innerHTML = `<div class="empty">지도를 불러오지 못했습니다(인터넷 연결 확인). 왼쪽 목록에서 지역을 고르세요.</div>`;
  } else {
    map = L.map(mapEl, { scrollWheelZoom: false, maxZoom: 13 }).setView([35.22, 128.62], 9);
    // 시안 v3 는 색이 옅고 한글 이름이 또렷한 Esri 지형도, 기본 화면은 기존 도로 지도
    const v3 = document.documentElement.hasAttribute("data-v3");
    L.tileLayer(`https://server.arcgisonline.com/ArcGIS/rest/services/${v3 ? "World_Topo_Map" : "World_Street_Map"}/MapServer/tile/{z}/{y}/{x}`, { maxZoom: 13, attribution: "Tiles &copy; Esri" }).addTo(map);
    const pts = [];
    for (const r of reg.regions) {
      if (!r.center) continue;
      const icon = L.divIcon({ className: "", html: `<div class="rpin${r.latest ? " done" : ""}">${esc(r.label)}</div>`, iconSize: [64, 30], iconAnchor: [32, 30] });
      pins[r.id] = L.marker(r.center, { icon, title: r.name }).addTo(map).on("click", () => pick(r.id));
      pts.push(r.center);
    }
    if (pts.length) map.fitBounds(pts, { padding: [50, 50], maxZoom: 10 });
    allPts = pts;
    dongLayer = L.layerGroup().addTo(map);
    setTimeout(() => map.invalidateSize(), 50);
  }

  // 고른 구역에 지도를 맞춘다: 구를 고르면 그 구의 동네들로, 동을 고르면 그 동으로 확대한다.
  // 동네는 점포 수에 비례한 점으로 그리고(점포가 적은 동은 흐리게), 점을 누르면 그 동을 고른다.
  function syncMap() {
    if (!map || !dongLayer) return;
    dongLayer.clearLayers();
    if (!sel) { if (allPts.length) map.flyToBounds(allPts, { padding: [50, 50], maxZoom: 10, duration: 0.6 }); return; }
    const tree = sel.tree;
    const use = sel.district ? tree.districts.filter((d) => d.name === sel.district) : tree.districts;
    const areas = use.flatMap((d) => d.areas.map((a) => ({ ...a, district: d.name }))).filter((a) => a.lat != null && a.lon != null);
    const max = Math.max(1, ...areas.map((a) => a.count));
    const isSel = (a) => sel.dong === a.name && (!tree.multi_district || sel.district === a.district);
    for (const a of areas) {
      const on = isSel(a);
      const dot = L.circleMarker([a.lat, a.lon], { radius: 5 + 11 * Math.sqrt(a.count / max), color: on ? "#3563e9" : "#1f2a44", weight: on ? 3 : 1,
        fillColor: on ? "#3563e9" : "#5b6b8c", fillOpacity: a.thin ? 0.25 : 0.55 }).addTo(dongLayer);
      dot.bindTooltip(`${a.name} · 점포 ${n(a.count)}개`, { direction: "top" });
      dot.on("click", () => {
        if (running || doneInfo) return;   // 의뢰 중·보고서 확인 전에는 지도에서 동을 골라도 바뀌지 않는다
        sel.dong = on ? "" : a.name;
        sel.district = tree.multi_district ? a.district : tree.districts[0].name;
        drawSide();
      });
    }
    const focus = areas.find(isSel);
    if (focus) map.flyTo([focus.lat, focus.lon], 13, { duration: 0.6 });
    else if (areas.length) map.flyToBounds(areas.map((a) => [a.lat, a.lon]), { padding: [60, 60], maxZoom: sel.district ? 12 : 11, duration: 0.6 });
  }

  async function pick(id) {
    const r = reg.regions.find((x) => x.id === id);
    if (!r || running) return;   // 의뢰 진행 중에는 다른 지역을 고를 수 없다
    if (sel && sel.r.id === id) { sel = null; drawSide(); return; }   // 선택한 지역을 다시 누르면 접는다
    try { trees[id] = trees[id] || (await api(`/api/regions/${encodeURIComponent(id)}/tree`)); } catch (err) { alert(err.message); return; }
    sel = { r, tree: trees[id], district: "", dong: "", large: "", mid: "", days: 14 };
    drawSide();
  }

  const root = $("#cmRoot");
  root.addEventListener("click", async (e) => {
    const dash = e.target.closest("[data-dash]");
    if (dash) { try { localStorage.setItem("dashRegion", dash.dataset.dash); } catch { /* 저장 못 해도 화면은 열린다 */ } return; }
    const dn = e.target.closest("[data-done]");
    if (dn) { $(dn.dataset.done === "open" ? "#openReport" : "#ackBtn")?.click(); return; }
    const crumb = e.target.closest("[data-crumb]");
    if (crumb) {
      if (running || doneInfo) return;   // 의뢰 중·보고서 확인 전에는 선택을 바꿀 수 없다
      const k = crumb.dataset.crumb;
      if (k === "root") sel = null;
      else if (sel && k === "city") { sel.district = ""; sel.dong = ""; }
      else if (sel && k === "district") sel.dong = "";
      drawSide();
      return;
    }
    const head = e.target.closest("[data-pick]");
    if (head) { pick(head.dataset.pick); return; }
    if (!sel) return;
    const c = e.target.closest("[data-city],[data-d],[data-a],[data-days]");
    if (c) {
      if (c.hasAttribute("data-city")) { sel.district = ""; sel.dong = ""; }
      else if (c.hasAttribute("data-d")) { sel.district = c.dataset.d; sel.dong = ""; }
      else if (c.hasAttribute("data-a")) { sel.dong = sel.dong === c.dataset.a ? "" : c.dataset.a; if (!sel.tree.multi_district) sel.district = sel.tree.districts[0].name; }
      else sel.days = Number(c.dataset.days);
      drawSide();
      return;
    }
    if (e.target.id === "goBtn" && !running) await commission();
  });
  root.addEventListener("change", (e) => {
    if (!sel) return;
    if (e.target.id === "upLarge") { sel.large = e.target.value; sel.mid = ""; drawSide(); }
    if (e.target.id === "upMid") { sel.mid = e.target.value; drawSide(); }
  });

  // 시안 v3: 처음 열면 리포트가 있는 지역(없으면 첫 지역)이 선택된 상태로 시작한다
  if (document.documentElement.hasAttribute("data-v3") && !running && !sel) pick((reg.regions.find((r) => r.latest) || reg.regions[0])?.id);

  // 시안 v3: 의뢰하기를 누르면 버튼을 잠그고, 가운데에 접수 애니메이션을 보여 준 뒤 사무실로 안내한다.
  function showSendOverlay(region, area, upjong) {
    const ov = document.createElement("div");
    ov.className = "send-ov";
    ov.setAttribute("role", "status");
    ov.innerHTML = `<div class="send-card">
      <div class="send-staff">${staff.map(() => `<span class="send-av"></span>`).join('<i class="send-arrow">→</i>')}</div>
      <h2 class="send-t">의뢰서를 전달하고 있어요</h2>
      <p class="send-s">${esc([region, area, upjong].filter(Boolean).join(" · "))}</p>
      <div class="send-bar"><i></i></div>
      <p class="send-n">잠시 후 사무실로 안내해 드릴게요</p></div>`;
    ov.querySelectorAll(".send-av").forEach((el, i) => { el.appendChild(avatarImg(staff[i].a.appearance, 56)); el.style.animationDelay = `${i * 0.18}s`; });
    document.body.appendChild(ov);
    requestAnimationFrame(() => ov.classList.add("on"));
    return {
      done() { ov.classList.add("done"); $(".send-t", ov).textContent = "의뢰서를 전달했어요"; $(".send-n", ov).textContent = "사무실로 이동합니다…"; },
      close() { ov.classList.remove("on"); setTimeout(() => ov.remove(), 250); },
    };
  }

  let sending = false;
  async function commission() {
    if (sending) return;
    sending = true;
    const s = span(), r = sel.r;
    const btn = $("#goBtn");
    if (btn) { btn.disabled = true; btn.textContent = "접수 중…"; }
    const v3 = document.documentElement.hasAttribute("data-v3");
    const ov = v3 ? showSendOverlay(r.name, areaText(), upText()) : null;
    const t0 = Date.now();
    try {
      const f = await api(`/api/offices/${office.id}/flows`, { method: "POST",
        body: { inputs: { window_start: s.start, window_end: s.end, region: r.name, area: areaText(), upjong: upText() }, auto_approve: true } });
      running = f.id; runRegion = r.name; runLabel = [r.name, areaText(), upText()].filter(Boolean).join(" · "); runStage = "의뢰서를 받았어요"; if (!v3) sel = null; drawSide(); track(f.id, r.name);
      if (ov) {
        await new Promise((res) => setTimeout(res, Math.max(0, 1800 - (Date.now() - t0))));   // 애니메이션을 최소 1.8초는 보여 준다
        ov.done();
        await new Promise((res) => setTimeout(res, 900));
        location.hash = `#/offices/${office.id}`;
        setTimeout(ov.close, 300);
      } else {
        $("#commissionState")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    } catch (err) {
      ov?.close();
      alert(err.message);
      const b2 = $("#goBtn");
      if (b2) { b2.disabled = false; b2.textContent = "이 조건으로 의뢰하기"; }
    } finally { sending = false; }
  }

  // 일하는 직원 표시: 실행 중인 부서의 직원은 말풍선과 들썩이는 동작, 대기 직원은 흐리게, 끝난 직원은 "다 했어요"
  const PHRASE = [[/화제/, "뉴스에서 화제 동네를 찾고 있어요"], [/테마/, "화제 동네를 테마로 묶고 있어요"], [/분석/, "상권 데이터를 세고 등급을 매기고 있어요"], [/편집/, "소상공인용 리포트를 쓰고 있어요"]];
  const RUNNING = ["running", "awaiting_approval", "paused"];
  function paintStaff(f) {
    const box = $("#staff");
    if (!box) return;
    const by = new Map();
    for (const d of f?.departments || []) for (const x of d.desks) {
      const e = by.get(x.agent_id) || { dept: d.name, xs: [] };
      e.xs.push(x); by.set(x.agent_id, e);
    }
    box.querySelectorAll(".staff-card").forEach((card) => {
      const b = $(".bubble", card);
      const e = by.get(card.dataset.agent);
      let st = "idle", text = "";
      if (f) {
        st = "wait";
        if (e && e.xs.length) {
          const total = e.xs.length, ok = e.xs.filter((x) => x.status === "ok").length;
          const running = e.xs.some((x) => RUNNING.includes(x.status));
          const failed = e.xs.some((x) => x.status === "failed" || x.status === "rejected");
          if (running) { st = "working"; text = (PHRASE.find(([re]) => re.test(e.dept)) || [0, "열심히 일하고 있어요"])[1] + (total > 1 ? ` (${ok}/${total})` : ""); }
          else if (failed) { st = "failed"; text = "앗, 문제가 생겼어요"; }
          else if (ok === total) { st = "done"; text = "다 했어요 ✓"; }
        }
      }
      card.dataset.state = st;
      b.hidden = !text;
      b.innerHTML = text ? `${esc(text)}${st === "working" ? '<span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>' : ""}` : "";
    });
    box.classList.toggle("busy", [...box.querySelectorAll(".bubble")].some((b) => !b.hidden));
  }

  // 진행 상황: 부서(직원)별 상태를 보여 주고, 끝나면 대시보드로 안내한다
  const ST = { pending: "대기", running: "일하는 중", ok: "완료", failed: "실패", not_covered: "일부 못 함", paused: "승인 대기" };
  // 시안 v3: 의뢰 중에는 왼쪽 조건 칸이 그대로 보이고 비활성화된다. 다른 화면에 갔다 와도 같은 모양이 되도록 실행의 입력값으로 선택을 복원한다.
  async function restoreSel(inp) {
    const r = reg.regions.find((x) => x.name === inp.region);
    if (!r) return;
    try { trees[r.id] = trees[r.id] || (await api(`/api/regions/${encodeURIComponent(r.id)}/tree`)); } catch { return; }
    if (sel) return;
    const tree = trees[r.id];
    const st = { r, tree, district: "", dong: "", large: "", mid: "", days: 14 };
    const area = String(inp.area || "").trim();
    if (area) {
      const parts = area.split(/\s+/);
      if (tree.multi_district) { st.district = parts[0] || ""; st.dong = parts.slice(1).join(" "); }
      else { st.district = tree.districts[0].name; st.dong = parts[0] || ""; }
    }
    const up = String(inp.upjong || "").trim();
    if (up) {
      const big = tree.upjong.find((u) => u.name === up);
      if (big) st.large = big.name;
      else { const hit = tree.upjong.find((u) => u.middle.some((m) => m.name === up)); if (hit) { st.large = hit.name; st.mid = up; } }
    }
    const d0 = inp.window_start && inp.window_end ? Math.round((new Date(inp.window_end) - new Date(inp.window_start)) / 86400000) : 0;
    if ([7, 14, 30].includes(d0)) st.days = d0;
    sel = st;
    drawSide();
  }

  const ackedId = () => { try { return localStorage.getItem("ackFlow"); } catch { return null; } };

  async function track(flowId, regionName) {
    const state = $("#commissionState");
    if (!state?.isConnected) return;
    let f;
    try { f = await api(`/api/offices/${office.id}/flows/${flowId}`); } catch { return; }
    const region = f.inputs?.region || regionName || "";
    const label = [region, f.inputs?.area, f.inputs?.upjong].filter(Boolean).join(" · ");
    const done = f.status !== "running";
    runLabel = label; runStage = f.stage || "";
    const v3t = document.documentElement.hasAttribute("data-v3");
    if (v3t) {
      const was = doneInfo && doneInfo.flowId;
      doneInfo = done && ackedId() !== flowId ? { status: f.status, label, flowId } : null;
      if (done && !sel && region) await restoreSel(f.inputs || {});
      if (done && (was !== (doneInfo && doneInfo.flowId)) && !running) drawSide();
    }
    const rb = $("#runBig");
    if (rb) { $(".rb-s", rb).textContent = runLabel; $(".rb-st", rb).textContent = runStage; }
    if (!done && runRegion !== region) { runRegion = region; drawSide(); }   // 시·군 타일에 '의뢰 중' 표시
    if (!done && !sel && document.documentElement.hasAttribute("data-v3") && region) await restoreSel(f.inputs || {});
    paintStaff(f);   // 끝난 뒤에도 사람이 확인할 때까지 직원 표시를 그대로 둔다
    const head = !done ? `${esc(label)} 조사를 진행하고 있어요`
      : f.status === "ok" ? `${esc(label)} 보고서를 작성했어요 ✓` : f.status === "stopped" ? `${esc(label)} 조사가 중단됐어요` : `${esc(label)} 조사에 문제가 생겼어요`;
    const sub = !done ? esc(f.stage || "") : f.status === "ok" ? "아래 버튼을 눌러 결과를 확인해 보세요." : esc(f.stage || "");
    state.innerHTML = `<div class="cstate ${f.status}">
      <div class="cstate-head"><b>${head}</b><span class="muted small">${sub}</span></div>
      ${f.error ? `<div class="flow-note bad">${esc(f.error)}</div>` : ""}
      <div class="cstate-actions">
        ${!done ? `<button type="button" class="danger" id="stopReq">⏹ 의뢰 중지</button>` : ""}
        ${done && f.status === "ok" ? `<button type="button" class="cta-ok" id="openReport">보고서 확인하기</button>` : ""}
        ${done && f.status !== "ok" ? `<a class="btn" href="#/offices/${esc(office.id)}/flows/${esc(flowId)}">실행 기록 보기</a>` : ""}
        <a class="btn" href="#/offices/${esc(office.id)}">🏢 사무실 방문</a>
        ${done ? `<button type="button" class="btn" id="ackBtn">닫기</button>` : ""}</div></div>`;
    $("#stopReq")?.addEventListener("click", async (e) => {
      if (!confirm("의뢰를 중지할까요?\n아직 안 보낸 직원은 일을 시작하지 않고, 이미 일하는 직원은 다음 단계를 멈춥니다.")) return;
      e.target.disabled = true; e.target.textContent = "중지하는 중…";
      try { await api(`/api/offices/${office.id}/flows/${flowId}/stop`, { method: "POST" }); } catch (err) { alert(err.message || "중지 요청 실패"); e.target.disabled = false; e.target.textContent = "⏹ 의뢰 중지"; }
    });
    const ack = () => { try { localStorage.setItem("ackFlow", flowId); } catch { /* 저장 못 해도 닫힌다 */ } state.innerHTML = ""; paintStaff(null); if (doneInfo) { doneInfo = null; drawSide(); } };
    $("#ackBtn")?.addEventListener("click", ack);
    $("#openReport")?.addEventListener("click", () => {   // 확인을 누르면 그 실행의 결과 대시보드로 간다
      try { localStorage.setItem("dashRegion", region); localStorage.setItem("dashFlow", flowId); localStorage.setItem("ackFlow", flowId); } catch { /* 무시 */ }
      doneInfo = null;   // 확인하면 다시 의뢰할 수 있는 상태로 초기화된다
      location.hash = "#/dashboard";
    });
    if (!done) { setTimeout(() => track(flowId, region), 3000); }
    else if (running === flowId) { running = null; runRegion = ""; try { reg.regions = (await api("/api/regions")).regions; } catch { /* 목록은 그대로 */ } drawSide(); }
  }

  drawSide();
  if (running) track(running, "");
  else {   // 다른 화면에 다녀와도 확인하지 않은 끝난 의뢰는 확인할 때까지 남아 있다(최근 24시간)
    let acked = null;
    try { acked = localStorage.getItem("ackFlow"); } catch { /* 없어도 된다 */ }
    try {
      const last = ((await api(`/api/offices/${office.id}/flows`)).flows || [])[0];
      const fin = last && Date.parse(last.finished_at || "");
      if (last && last.status !== "running" && last.id !== acked && fin && Date.now() - fin < 86400000) track(last.id, "");
    } catch { /* 실행 기록을 못 불러와도 화면은 쓴다 */ }
  }
}

// 전문자료 개수를 한 줄로. 없으면 아무것도 그리지 않는다(자리는 유지).
function docsBadge(docs, agentId = null) {
  const el = document.createElement("div");
  el.className = "docs-badge";
  if (agentId) el.dataset.agent = agentId;
  if (!docs || !docs.total) return el;
  const parts = [`📄 전문자료 ${docs.ready}/${docs.total}`];
  if (docs.processing) parts.push("분석 중…");
  if (docs.failed) parts.push(`실패 ${docs.failed}`);
  el.textContent = parts.join(" · ");
  el.classList.toggle("busy", docs.processing > 0);
  el.classList.toggle("bad", docs.failed > 0 && !docs.processing);
  return el;
}

// 답변 아래에 붙는 출처 목록. 눌러서 펼치면 실제로 참고한 발췌를 볼 수 있다.
function sourcesBlock(sources) {
  const docs = sources.filter((h) => h.via !== "graph-info" && h.via !== "tool");
  const info = sources.find((h) => h.via === "graph-info");
  const toolCalls = sources.filter((h) => h.via === "tool");
  const viaGraph = docs.filter((h) => h.via === "graph").length;
  const box = document.createElement("details");
  box.className = "sources";
  box.innerHTML = `<summary>${[docs.length || !(info || toolCalls.length) ? `참고한 자료 ${docs.length}건${viaGraph ? ` (그래프로 보강 ${viaGraph}건)` : ""}` : "", info ? "지식 그래프 정보" : "", toolCalls.length ? `도구 사용 ${toolCalls.length}회` : ""].filter(Boolean).join(" · ")}</summary>`
    + toolCalls.map((c) => `<div class="src src-tool${c.ok ? "" : " src-tool-bad"}"><div class="src-head"><b>도구 사용: ${esc(c.label || c.tool)}</b> <span class="badge-tool">${c.ok ? "성공" : "실패"}</span>
        <span class="muted small">${esc(Object.entries(c.args || {}).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", "))} · ${c.ms ?? 0}ms</span></div>
        <div class="small">${esc(c.summary)}</div>
        ${(c.items || []).map((i) => `<div class="small">[${esc(i.w)}] ${/^https?:\/\//.test(i.url || "") ? `<a href="${esc(i.url)}" target="_blank" rel="noopener noreferrer">${esc(i.title || i.url)}</a>` : esc(i.title || i.url)}${i.date ? ` <span class="muted">(${esc(i.date)})</span>` : ""}</div>`).join("")}
        ${c.ok && !(c.items || []).length ? `<div class="src-text">${esc(c.text)}</div>` : ""}</div>`).join("")
    + (info ? `<div class="src src-graph"><div class="src-head"><b>지식 그래프 정보</b> <span class="badge-graph">그래프</span>
        <span class="muted small">질문에 나온 개체: ${esc((info.entities || []).join(", "))}</span>
        ${(info.semantic || []).length ? `<span class="muted small">· 의미가 비슷해 찾음: ${esc(info.semantic.map((x) => `${x.name} (${x.sim})`).join(", "))}</span>` : ""}</div>
        <div class="src-text">${esc(info.snippet)}</div></div>` : "")
    + docs.map((h) => `
    <div class="src"><div class="src-head"><b>[${h.n}] ${esc(h.filename)}</b>${h.page ? ` · ${h.page}쪽` : ""}
      ${h.via === "graph" ? `<span class="badge-graph">그래프로 보강</span>` : ""}
      ${h.sim != null ? `<span class="muted small">유사도 ${h.sim}</span>` : ""}</div>
      <div class="src-text">${esc(h.snippet)}</div></div>`).join("");
  return box;
}

// ---------------------------------------------------------------- 대화

async function renderChat(agentId) {
  const [agent, messages] = await Promise.all([
    api(`/api/agents/${agentId}`),
    api(`/api/agents/${agentId}/messages`),
  ]);

  app.innerHTML = `
    <div class="chat">
      <div class="chat-head">
        <div class="slot"></div>
        <div class="who"><b>${esc(staffName(agent))}</b><span>${esc(agent.role)}${agent.tagline ? " · " + esc(agent.tagline) : ""}${document.documentElement.hasAttribute("data-v3") ? "" : " · " + esc(modelTag(agent.model))}</span></div>
        <label class="chat-report" id="chatReportWrap" hidden><span>참고 보고서</span><select id="chatReport" aria-label="대화에 참고할 내가 쓴 보고서"></select></label>
        <span id="chatDocs" class="docs-badge"></span>
        <button id="profileBtn" title="이 직원이 하는 일·전문자료·지식 그래프를 봅니다">직원 소개</button>
        <button id="clearBtn">대화 지우기</button>
        <button id="backBtn">목록</button>
      </div>
      <div id="chatBody" class="chat-body"></div>
      <form id="chatForm" class="chat-input">
        <textarea id="chatInput" rows="1" maxlength="8000" placeholder="메시지를 입력하세요 (Enter 전송, Shift+Enter 줄바꿈)"></textarea>
        <button class="primary" type="submit">보내기</button>
      </form>
    </div>`;
  $(".slot").replaceWith(avatarImg(agent.appearance, 48));

  const body = $("#chatBody");
  const input = $("#chatInput");
  let busy = false;
  // 대화가 끝나면(다른 화면으로 가거나 창을 닫으면) 이 직원과의 대화 기록을 지운다 — 매번 새 대화로 시작한다.
  const chatHash = `#/chat/${agentId}`;
  const wipeChat = () => { try { fetch(`/api/agents/${agentId}/messages`, { method: "DELETE", keepalive: true }); } catch { /* 지우지 못해도 화면은 계속 쓸 수 있다 */ } };
  const onLeaveChat = () => {
    if (location.hash === chatHash) return;
    window.removeEventListener("hashchange", onLeaveChat);
    window.removeEventListener("pagehide", wipeChat);
    wipeChat();
  };
  window.addEventListener("hashchange", onLeaveChat);
  window.addEventListener("pagehide", wipeChat);
  // 「내가 쓴 보고서」: 이 직원이 쓴 실행 결과를 대화 문맥으로 임시로 붙인다(기록에는 저장되지 않는다). 기본은 가장 최근 보고서.
  let reportCtx = { office_id: "", flow_id: "" };
  (async () => {
    try {
      const reg = await api("/api/regions");
      const reps = (await api(`/api/offices/${reg.office_id}/reports`)).reports || [];
      if (!reps.length) return;
      const key = `chatReport:${agentId}`;
      let saved = null;
      try { saved = localStorage.getItem(key); } catch { /* 저장소 없이도 동작 */ }
      const sel = $("#chatReport");
      const stamp = (iso) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? "" : `${d.getMonth() + 1}/${d.getDate()}`; };
      sel.innerHTML = `<option value="">보고서 없이 대화</option>` + reps.map((r) => `<option value="${esc(r.flow_id)}">${esc(stamp(r.finished_at))} · ${esc(r.scope)}</option>`).join("");
      sel.value = saved !== null && (saved === "" || reps.some((r) => r.flow_id === saved)) ? saved : reps[0].flow_id;
      const apply = () => { reportCtx = { office_id: sel.value ? reg.office_id : "", flow_id: sel.value }; try { localStorage.setItem(key, sel.value); } catch { /* 무시 */ } };
      sel.addEventListener("change", apply);
      apply();
      $("#chatReportWrap").hidden = false;
    } catch { /* 보고서를 못 불러와도 대화는 된다 */ }
  })();

  const scrollDown = () => { body.scrollTop = body.scrollHeight; };
  const v3chat = document.documentElement.hasAttribute("data-v3");
  const bubble = (role, text, extra = "") => {
    const div = document.createElement("div");
    div.className = `bubble ${role} ${extra}`.trim();
    div.textContent = text;
    body.appendChild(div);
    scrollDown();
    return div;
  };
  // 시안 v3: 직원 캐릭터가 대화창 왼쪽 아래에 고정되어 서 있고, 가장 최근 답을 그 옆 말풍선으로 말한다.
  // 두 번째 답부터는 앞의 답이 위쪽 일반 채팅 기록으로 올라간다.
  const stage = v3chat ? document.createElement("div") : null;
  let say = null, saySrc = null, charEl = null, stageAns = null;
  if (stage) {
    stage.className = "chat-stage";
    charEl = document.createElement("div");
    charEl.className = "chat-char";
    charEl.appendChild(avatarImg(agent.appearance, 280, { transparent: true }));
    charEl.insertAdjacentHTML("beforeend", '<span class="chat-monitor"></span><span class="chat-desk"></span>');   // 책상에 앉아 있는 모습
    const wrap = document.createElement("div");
    wrap.className = "stage-wrap";
    say = document.createElement("div");
    say.className = "bubble assistant stage-say";
    saySrc = document.createElement("div");
    saySrc.className = "stage-src";
    wrap.append(say, saySrc);
    const plant = document.createElement("div"); plant.className = "chat-plant";
    plant.innerHTML = '<svg viewBox="0 0 80 140" width="80" height="140" aria-hidden="true"><path d="M40 92 C38 70 26 58 14 54 C22 74 30 84 40 92Z" fill="#3f9d4b"/><path d="M40 92 C42 62 54 46 68 40 C62 66 54 82 40 92Z" fill="#58b85f"/><path d="M40 92 C36 60 38 38 46 20 C52 42 48 70 40 92Z" fill="#7bd17f"/><path d="M40 92 C28 84 14 80 6 82 C16 92 28 96 40 92Z" fill="#2f8a3e"/><path d="M40 92 C52 82 64 80 74 84 C64 94 52 97 40 92Z" fill="#4caa56"/><path d="M14 100 H66 L60 134 Q59 138 55 138 H25 Q21 138 20 134Z" fill="#f4f1ea" stroke="#d6d1c4" stroke-width="2"/><rect x="11" y="94" width="58" height="12" rx="5" fill="#b8f36b" stroke="#8fcb45" stroke-width="2"/></svg>';
    stage.append(charEl, wrap, plant);
    $("#chatBody").before(stage);   // 캐릭터는 위, 대화 기록은 그 아래
  }
  const think = (on) => charEl?.classList.toggle("thinking", on);
  $("#chatDocs").replaceWith(Object.assign(docsBadge(agent.docs), { id: "chatDocs" }));
  const addSources = (sources) => {
    if (!sources?.length) return;
    const el = sourcesBlock(sources);
    body.appendChild(el);
  };
  const showHint = () => {
    if (stage) {
      stageAns = null;
      say.className = "bubble assistant stage-say";
      say.textContent = `${staffName(agent)}입니다. 먼저 말을 걸어보세요. 업무 능력을 물어보거나 간단한 일을 시켜볼 수 있어요.`;
      saySrc.replaceChildren();
      return;
    }
    body.innerHTML = `<div class="chat-hint">${esc(staffName(agent))}에게 먼저 말을 걸어보세요.<br>업무 능력을 물어보거나 간단한 일을 시켜볼 수 있어요.</div>`;
  };
  const showAnswer = (text, sources) => {
    say.className = "bubble assistant stage-say";
    say.textContent = text;
    say.scrollTop = 0;
    saySrc.replaceChildren();
    if (sources?.length) saySrc.appendChild(sourcesBlock(sources));
    stageAns = { text, sources };
  };
  const archiveAnswer = () => {   // 말풍선에 있던 답을 위쪽 일반 채팅 기록으로 올린다
    if (!stageAns) return null;
    const a = stageAns, els = [bubble("assistant", a.text)];
    if (a.sources?.length) { const e = sourcesBlock(a.sources); body.appendChild(e); els.push(e); }
    stageAns = null;
    return { a, els };
  };

  if (stage) {
    const last = messages.length ? messages[messages.length - 1] : null;
    const lastAns = last && last.role === "assistant" ? last : null;
    messages.forEach((m) => { if (m === lastAns) return; bubble(m.role, m.content); if (m.role === "assistant") addSources(m.sources); });
    if (lastAns) showAnswer(lastAns.content, lastAns.sources); else showHint();
  } else if (messages.length) messages.forEach((m) => { bubble(m.role, m.content); if (m.role === "assistant") addSources(m.sources); }); else showHint();

  async function send() {
    const text = input.value.trim();
    if (!text || busy) return;
    busy = true;
    $("#chatForm button").disabled = true;
    $(".chat-hint", body)?.remove();
    input.value = "";
    autosize();
    const arch = stage ? archiveAnswer() : null;   // 앞의 답은 위 기록으로
    bubble("user", text);
    const waitText = (agent.tools || []).length ? "자료를 찾아보는 중" : "생각하는 중";
    let pending = null;
    if (stage) { say.className = "bubble assistant stage-say pending"; say.textContent = waitText; saySrc.replaceChildren(); }
    else pending = bubble("assistant", (agent.tools || []).length ? "… (도구를 쓰면 시간이 더 걸릴 수 있어요)" : "…", "pending");
    think(true);
    try {
      const reply = await api(`/api/agents/${agentId}/messages`, { method: "POST", body: { content: text, report_office_id: reportCtx.office_id || null, report_flow_id: reportCtx.flow_id || null } });
      if (stage) showAnswer(reply.content, reply.sources);
      else { pending.textContent = reply.content; pending.classList.remove("pending"); addSources(reply.sources); }
      think(false);
      if (reply.notice) bubble("notice", reply.notice);
      scrollDown();
    } catch (err) {
      // 실패한 턴은 서버 기록에 남지 않는다. 보낸 말풍선을 치우고 입력창에 되돌려 다시 보낼 수 있게 한다.
      think(false);
      if (stage) {
        body.lastElementChild?.remove();                 // 방금 보낸 내 말
        arch?.els.forEach((e) => e.remove());            // 위로 올렸던 앞의 답
        if (arch) showAnswer(arch.a.text, arch.a.sources); else showHint();
      } else { pending.remove(); body.lastElementChild?.remove(); }
      bubble("error", err.message);
      input.value = text;
      autosize();
    } finally {
      busy = false;
      $("#chatForm button").disabled = false;
      input.focus();
    }
  }

  const autosize = () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  };
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => {
    // 한글 입력 중(조합 중)에 누른 Enter는 글자 확정용이라 전송하지 않는다
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  $("#chatForm").addEventListener("submit", (e) => { e.preventDefault(); send(); });

  $("#clearBtn").addEventListener("click", async () => {
    if (busy || !confirm("이 캐릭터와의 대화 기록을 모두 지울까요?")) return;
    try {
      await api(`/api/agents/${agentId}/messages`, { method: "DELETE" });
      body.replaceChildren();
      showHint();
    } catch (err) { alert(err.message); }
  });
  $("#backBtn").addEventListener("click", () => { location.hash = "#/agents"; });
  $("#profileBtn").addEventListener("click", () => { location.hash = `#/agents/${agentId}`; });

  input.focus();
  scrollDown();
}

// ---------------------------------------------------------------- 시작

(async function init() {
  try {
    const h = await api("/api/health");
    $("#mode").textContent = h.llm_mode;
  } catch { /* 서버 상태 배지는 없어도 동작에 지장 없음 */ }
  route();
})();
