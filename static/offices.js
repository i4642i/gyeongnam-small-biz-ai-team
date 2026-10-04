// 직원 표시 이름(시안 v3): 사용자가 정한 이름·직급. 직원 설정(이름·프롬프트)은 바꾸지 않고 화면에만 쓴다.
const STAFF_ALIAS = { a_7612ec55: "김쫑긋 사원", a_bcb2ef08: "박이음 대리", a_a28223e1: "오꼼꼼 과장", a_b5f24ab2: "정또박 팀장" };
const staffName = (a) => (document.documentElement.hasAttribute("data-v3") && STAFF_ALIAS[a.id]) || a.name;

// 회사 화면:
//   #/offices             사무실(첫 회사 화면으로 바로 이동)
//   #/offices/{id}        회사 배치 (책상에 캐릭터 앉히기)
// $, api, esc, app 은 app.js 에서 온다.

const MAX_DEPARTMENTS = 12;
const MAX_DESKS = 30;      // 부서 하나당 (서버의 MAX_DESKS 와 같은 값)

const totalDesks = (departments) => departments.reduce((sum, d) => sum + d.desks, 0);
const summaryText = (departments) => `부서 ${departments.length}개 · 책상 ${totalDesks(departments)}개`;

// 부서별 책상 배치도. 만들기 미리보기와 목록 카드가 같이 쓴다.
// departments: [{ id?, name, desks }]  (미리보기에서는 입력 도중의 값이라 desks 가 0일 수 있다)
// seating:     { assignments, agentsById } — 있으면 앉은 캐릭터를 책상 대신 아바타로 그린다
// 실제로 존재하는 캐릭터가 앉은 책상 수 (삭제된 캐릭터의 흔적은 세지 않는다)
const seatedCount = (office, agentsById) => Object.values(office.assignments).filter((id) => agentsById.has(id)).length;
const byId = (agents) => new Map(agents.map((a) => [a.id, a]));

// ---------------------------------------------------------------- 사무실 (회사 목록 없이 바로 그 회사 화면으로)

async function renderOfficeList() {
  const offices = await api("/api/offices");
  if (!offices.length) { app.innerHTML = `<div class="empty">사무실이 없습니다.</div>`; return; }
  location.replace(`#/offices/${offices[0].id}`);
}

// ---------------------------------------------------------------- 회사 배치 (책상에 캐릭터 앉히기)

async function renderOfficeDetail(officeId) {
  let office, agents, agentsById;
  let computers = [];   // 이 회사에 놓인 컴퓨터(설비실에 그린다)
  let flowState = null;   // { config, plan, running } — 흐름 설정·실행 상태
  let runTimer = null;
  let apprSig = "";   // 결재 대기 목록의 현재 상태 서명 — 바뀔 때만 다시 그린다(펼친 프롬프트 유지)

  const load = async () => {
    const [o, list, libs] = await Promise.all([api(`/api/offices/${officeId}`), api("/api/agents"), api("/api/libraries")]);
    office = o;
    computers = libs.libraries.filter((l) => (o.libraries || []).includes(l.id));
    agents = list;
    agentsById = byId(list);
  };
  await load();

  app.innerHTML = `
    <div class="page-head head-row">
      <div>
        <h1 id="oTitle"></h1>
        <p id="oGoal"></p>
        <p id="oCount" class="muted"></p>
      </div>
    </div>
    <section class="run-form" id="officeRun"></section>
    <div id="seatBody" class="stage"></div>
    <section class="flow-section" id="approvalArea" hidden><h3>결재 대기 — 프롬프트 확인 후 승인해야 전송됩니다</h3><div id="approvalBody"></div></section>
    <section class="flow-section"><h3>최근 실행</h3><div id="officeHistory"><span class="muted small">불러오는 중…</span></div></section>`;
  app.classList.add("wide");
  $("#oTitle").textContent = office.name;
  $("#oGoal").textContent = office.goal;
  $("#oGoal").hidden = !office.goal;

  const alive = () => document.body.contains($("#officeRun"));

  // ---------------------------------------------------------------- 회사 실행 (그림 화면에서 바로)
  async function loadFlow() {
    try { flowState = await api(`/api/offices/${officeId}/flow`); } catch { flowState = null; }
  }

  // 회사 실행 입력 폼은 없앴다(2026-09-30): 의뢰는 상담소에서 한다. 여기서는 흐름 설정 문제가 있을 때와
  // 실행 중일 때(진행 상태 보기·작업 정지)만 얇은 줄로 보여 준다.
  function drawRun() {
    if (!alive()) return;
    const box = $("#officeRun");
    const p = flowState?.plan;
    const busy = !!flowState?.running;
    const problems = p ? [
      ...p.problems.map((x) => `<div class="flow-note bad">⚠ ${esc(x)}</div>`),
      ...p.warnings.map((x) => `<div class="flow-note">ℹ ${esc(x)}</div>`),
    ].join("") : "";
    box.hidden = !busy && !problems;
    if (box.hidden) { box.innerHTML = ""; return; }
    box.innerHTML = `
      ${problems}
      ${busy ? `<div class="submit-bar"><span class="muted small">상담소에서 의뢰한 조사가 진행 중입니다.</span>
        <a class="pill busy" href="#/offices/${officeId}/flows/${flowState.running}">실행 기록 보기</a>
        <button class="danger" id="stopBtn" title="지금 도는 흐름을 멈춥니다. 아직 안 보낸 책상은 안 보내고, 이미 도는 책상은 다음 모델 왕복을 취소합니다.">⏹ 작업 정지</button></div>` : ""}`;
    const stopBtn = $("#stopBtn");
    if (stopBtn) stopBtn.addEventListener("click", async () => {
      if (!confirm("지금 도는 작업을 정지할까요?\n아직 안 보낸 책상은 보내지 않고, 이미 도는 책상은 다음 모델 왕복을 취소합니다(진행 중이던 한 번은 끝납니다).")) return;
      stopBtn.disabled = true;
      try {
        await api(`/api/offices/${officeId}/flows/${flowState.running}/stop`, { method: "POST" });
      } catch (err) {
        alert(err.message || "정지 요청 실패");
        stopBtn.disabled = false;
      }
    });
  }

  // 책상 상태등: 실행 중인 흐름의 책상별 상태를 자리 위에 표시한다
  function paintSeatLights(deskStatus) {
    document.querySelectorAll("#seatBody .seat").forEach((seat) => {
      const key = `${seat.dataset.dept}:${seat.dataset.desk}`;
      const st = deskStatus.get(key);
      if (st) seat.dataset.run = st; else delete seat.dataset.run;
    });
  }


  // ---------------------------------------------------------------- 실시간 말풍선(시안 v3)
  // 일하는 직원과 컴퓨터 위에 지금 무슨 일을 하는지 말풍선으로 보여 준다. 글은 실행이 알려 주는 실제 단계(stage)와 문서 수에서 만든다.
  const SAY_ROLE = [[/화제/, "뉴스에서 화제 동네를 찾고 있어요"], [/테마/, "화제 동네를 테마로 묶고 있어요"], [/분석/, "상가 데이터를 세고 등급을 매기고 있어요"], [/편집/, "소상공인용 리포트를 쓰고 있어요"]];
  const SAY_STAGE = [[/자료 검색/, "자료를 찾아 읽으며 쓰는 중"], [/형식 검사/, "결과를 점검하는 중"], [/다시 받는/, "형식을 고쳐 다시 받는 중"], [/이어서/, "이어서 쓰는 중"], [/잘림/, "길어서 다시 쓰는 중"], [/승인 대기/, "승인을 기다리는 중"]];
  const liveOn = () => document.documentElement.hasAttribute("data-v3");
  function sayBubble(host, kind, text, sub) {
    let el = host.querySelector(":scope > .live-say");
    if (!el) { el = document.createElement("div"); el.className = "live-say"; host.prepend(el); }
    const key = `${kind}|${text}|${sub || ""}`;
    if (el.dataset.key === key) return;
    el.dataset.key = key;
    el.className = `live-say ${kind}`;
    el.innerHTML = `<b>${esc(text)}${kind === "work" ? '<span class="dots"><i></i><i></i><i></i></span>' : ""}</b>${sub ? `<small>${esc(sub)}</small>` : ""}${kind === "work" ? '<u class="sp"></u><u class="sp"></u><u class="sp"></u>' : ""}`;
  }
  function clearLive() {
    const st = $("#seatBody");
    if (!st) return;
    st.classList.remove("live");
    st.querySelectorAll(".live-say").forEach((e) => e.remove());
    st.querySelectorAll(".stage-arrow.go, .stage-arrow.passed, .stage-result.ready, .equip.busy, .room.working").forEach((e) => e.classList.remove("go", "passed", "ready", "busy", "working"));
  }
  async function paintLive(f) {
    if (!liveOn()) return;
    const st = $("#seatBody");
    if (!st) return;
    const running = f.status === "running";
    st.classList.toggle("live", running);
    const rooms = [...st.querySelectorAll(".stage-staff > .room")];
    const states = [];
    rooms.forEach((room) => {
      const dept = room.querySelector(".seat")?.dataset.dept;
      const d = f.departments.find((x) => x.id === dept);
      if (!d) { states.push("none"); return; }
      const total = d.desks.length;
      const run = d.desks.filter((x) => ["running", "awaiting_approval", "paused"].includes(x.status));
      const ok = d.desks.filter((x) => ["ok", "warning"].includes(x.status)).length;
      const bad = d.desks.filter((x) => ["error", "check_failed", "stopped"].includes(x.status)).length;
      const name = d.name || "";
      let state = "wait";
      if (run.length) {
        state = "run";
        const stage = run[0].stage || "";
        const sub = (SAY_STAGE.find(([re]) => re.test(stage)) || [0, ""])[1];
        sayBubble(room, "work", (SAY_ROLE.find(([re]) => re.test(name)) || [0, "열심히 일하고 있어요"])[1] + (total > 1 ? ` (${ok}/${total})` : ""), sub);
      } else if (bad) { state = "bad"; sayBubble(room, "fail", "문제가 생겼어요", "사무실 실행 기록에서 확인하세요"); }
      else if (total && ok === total) { state = "ok"; sayBubble(room, "done", "다 했어요 ✓", ""); }
      else if (running) sayBubble(room, "wait", "차례를 기다리고 있어요", "");
      room.classList.toggle("working", state === "run");
      states.push(state);
    });
    // 화살표: 다음 부서가 일하면 신호가 흐르고, 지나간 화살표는 표시를 남긴다
    st.querySelectorAll(".stage-staff > .stage-arrow").forEach((ar, k) => {
      const nxt = states[k + 1];
      const resultArrow = k >= rooms.length - 1;
      ar.classList.toggle("go", resultArrow ? (f.status === "ok" || f.status === "warning") : nxt === "run");
      ar.classList.toggle("passed", resultArrow ? false : nxt === "run" || nxt === "ok");
    });
    st.querySelector(".stage-result")?.classList.toggle("ready", f.status === "ok" || f.status === "warning");
    // 컴퓨터: 시작 전 준비(기사 수집·색인) 중이거나 화제 탐지부가 일할 때, 그 지역 컴퓨터에 말풍선
    let libs = [];
    try { libs = (await api("/api/libraries")).libraries || []; } catch { /* 문서 수는 못 가져와도 말풍선은 보인다 */ }
    if (!alive()) return;
    const prep = f.prepare || null;
    const inPrep = running && /시작 전 준비/.test(f.stage || "") && prep;
    const usingPc = running && !inPrep && prep && states[0] === "run";
    st.querySelectorAll(".equip").forEach((eq) => {
      const id = eq.dataset.pc;
      const lib = libs.find((l) => l.id === id);
      const mine = prep && prep.computer === id;
      eq.classList.toggle("busy", !!(mine && (inPrep || usingPc)) || !!(lib && (lib.state === "busy" || lib.active_jobs > 0)));   // 기사를 받거나 색인하는 컴퓨터는 흔들린다
      if (mine && inPrep) {
        const idx = lib && lib.processing > 0;
        sayBubble(eq, "work", idx ? "기사를 검색할 수 있게 정리하는 중" : "뉴스 기사를 모으는 중", lib ? (idx ? `준비 ${lib.ready}건 · 처리 중 ${lib.processing}건` : `지금까지 ${lib.documents}건`) : "");
      } else if (mine && usingPc) sayBubble(eq, "work", "직원이 기사를 찾아 읽는 중", lib ? `보관 기사 ${lib.documents}건` : "");
      else eq.querySelector(":scope > .live-say")?.remove();
    });
  }

  // 시안 v3: 이 화면에서 일하는 모습을 보다가 보고서가 완성되면 축하 효과를 보여 주고 열람실로 안내한다
  let sawRunning = false;
  function celebrateAndGo(f) {
    if (document.querySelector(".done-ov")) return;
    const region = f.inputs?.region || "";
    const label = [region, f.inputs?.area, f.inputs?.upjong].filter(Boolean).join(" · ");
    const ov = document.createElement("div");
    ov.className = "done-ov";
    ov.setAttribute("role", "status");
    ov.innerHTML = `<div class="done-card"><div class="dc-paper"><i></i><i></i><i></i><b>✓</b></div>
      <h2>보고서가 완성됐어요!</h2><p>${esc(label)}</p><div class="dc-bar"><i></i></div><small>열람실로 이동합니다…</small>
      ${Array.from({ length: 14 }, (_, k) => `<u class="cf" style="--x:${(k * 37) % 100}%;--d:${(k % 7) * 0.12}s;--r:${(k * 53) % 360}deg"></u>`).join("")}</div>`;
    document.body.appendChild(ov);
    requestAnimationFrame(() => ov.classList.add("on"));
    try { localStorage.setItem("dashRegion", region); localStorage.setItem("dashFlow", f.id); localStorage.setItem("ackFlow", f.id); } catch { /* 저장 못 해도 이동은 된다 */ }
    setTimeout(() => { location.hash = "#/dashboard"; setTimeout(() => { ov.classList.remove("on"); setTimeout(() => ov.remove(), 300); }, 350); }, 2600);
  }

  async function pollRun(flowId) {
    if (runTimer) clearTimeout(runTimer);
    let f;
    try { f = await api(`/api/offices/${officeId}/flows/${flowId}`); } catch { return; }
    if (!alive()) return;
    const deskStatus = new Map();
    for (const d of f.departments) for (const x of d.desks) deskStatus.set(x.key, x.status);
    paintSeatLights(deskStatus);
    paintLive(f);
    renderApproval(flowId, f);
    if (f.status === "running") sawRunning = true;
    if (liveOn() && sawRunning && f.status === "ok") { sawRunning = false; celebrateAndGo(f); }
    if (f.status === "running") {
      runTimer = setTimeout(() => pollRun(flowId), 2000);
    } else {
      flowState.running = null;
      await loadFlow();
      drawRun();
      setTimeout(() => alive() && (paintSeatLights(new Map()), clearLive()), 6000);   // 상태등·말풍선을 잠시 보여준 뒤 지운다
    }
  }

  // 반복 부서 책상이면 무엇을 맡았는지(예: 종목 코드)를 결재 카드에 보인다
  const apprItem = (x) => { const it = x.repeat_item; return it && typeof it === "object" ? (it.ticker || it.name || "") : (typeof it === "string" ? it : ""); };
  // 결재 대기 구역: 부서별로, 프롬프트가 준비돼 승인을 기다리는 책상을 목록으로 보여준다.
  function renderApproval(flowId, f) {
    const area = $("#approvalArea"), body = $("#approvalBody");
    if (!area || !body) return;
    const waiting = [];
    for (const d of f.departments) for (const x of d.desks) if (x.status === "awaiting_approval") waiting.push({ dept: d, desk: x });
    if (!waiting.length) { area.hidden = true; apprSig = ""; return; }
    area.hidden = false;
    // 2초마다 폴링이 이 함수를 다시 부른다. 대기 목록이 그대로면 다시 그리지 않는다 —
    // 안 그러면 사용자가 펼쳐 둔 "프롬프트 보기"가 매번 초기화돼 닫혀 버린다.
    const sig = waiting.map((w) => `${w.desk.key}:${w.desk.status}:${w.desk.prompt_ready}:${(w.desk.upstream_warnings || []).length}`).join("|");
    if (sig === apprSig) return;
    apprSig = sig;
    // 부서별 묶음
    const byDept = new Map();
    for (const w of waiting) { if (!byDept.has(w.dept.id)) byDept.set(w.dept.id, { name: w.dept.name, desks: [] }); byDept.get(w.dept.id).desks.push(w.desk); }
    body.innerHTML = [...byDept.values()].map((g) => `
      <div class="appr-dept"><div class="appr-dept-name">${esc(g.name)} <span class="muted small">· 승인 대기 ${g.desks.length}</span></div>
        ${g.desks.map((x) => `
          <div class="appr-desk" data-key="${esc(x.key)}">
            <div class="appr-desk-head"><b>${esc(x.agent_name || x.agent_role || x.key)}</b>${apprItem(x) ? ` <span class="pill">${esc(apprItem(x))}</span>` : ""} <span class="muted small">${esc(x.task_name || "")}</span>
              ${x.prompt_ready ? "" : `<span class="muted small">· 프롬프트 생성 실패</span>`}</div>
            ${(x.upstream_warnings || []).length ? `<div class="notice warn small appr-warn"><b>앞 부서에서 경고 통과로 넘어온 결과 ${x.upstream_warnings.reduce((n, w) => n + w.items.length, 0)}건</b> — 형식 허용 목록(인용 길이·서버가 채우는 칸)만 해당합니다. 이 책상의 입력에 들어갑니다.
              <ul>${x.upstream_warnings.map((w) => `<li><b>${esc(w.desk)}</b>: ${w.items.map(esc).join(" / ")}</li>`).join("")}</ul></div>` : ""}
            <div class="appr-actions">
              <button type="button" class="link" data-appr-view="${esc(x.key)}">프롬프트 보기</button>
              ${x.prompt_ready ? `
              <button type="button" class="link" data-appr-copy="${esc(x.key)}">복사하기</button>
              <a class="link" href="/api/offices/${officeId}/flows/${flowId}/desks/${encodeURIComponent(x.key)}/prompt?download=true" download>txt 내려받기</a>` : ""}
              <button type="button" class="primary" data-appr-ok="${esc(x.key)}">승인 · 전송</button>
              <button type="button" class="danger" data-appr-no="${esc(x.key)}">거부</button>
            </div>
            <pre class="appr-prompt" data-appr-prompt="${esc(x.key)}" hidden></pre>
          </div>`).join("")}
      </div>`).join("");
    body.querySelectorAll("[data-appr-view]").forEach((b) => b.addEventListener("click", async () => {
      const key = b.dataset.apprView, pre = body.querySelector(`[data-appr-prompt="${CSS.escape(key)}"]`);
      if (!pre.hidden) { pre.hidden = true; return; }
      pre.hidden = false; pre.textContent = "불러오는 중…";
      try { const r = await fetch(`/api/offices/${officeId}/flows/${flowId}/desks/${encodeURIComponent(key)}/prompt`); pre.textContent = r.ok ? await r.text() : "프롬프트를 불러오지 못했습니다."; }
      catch { pre.textContent = "프롬프트를 불러오지 못했습니다."; }
    }));
    body.querySelectorAll("[data-appr-copy]").forEach((b) => b.addEventListener("click", async () => {
      const key = b.dataset.apprCopy, label = b.textContent;
      try {
        const r = await fetch(`/api/offices/${officeId}/flows/${flowId}/desks/${encodeURIComponent(key)}/prompt`);
        if (!r.ok) throw new Error("불러오기 실패");
        await navigator.clipboard.writeText(await r.text());
        b.textContent = "복사됨 ✓";
        setTimeout(() => { b.textContent = label; }, 1500);
      } catch { alert("프롬프트를 복사하지 못했습니다."); }
    }));
    const act = (key, kind) => api(`/api/offices/${officeId}/flows/${flowId}/desks/${encodeURIComponent(key)}/${kind}`, { method: "POST" });
    body.querySelectorAll("[data-appr-ok]").forEach((b) => b.addEventListener("click", async () => { b.disabled = true; try { await act(b.dataset.apprOk, "approve"); } catch (e) { alert(e.message); b.disabled = false; } }));
    body.querySelectorAll("[data-appr-no]").forEach((b) => b.addEventListener("click", async () => { if (!confirm("이 책상을 거부할까요? 전송하지 않고 실패로 남깁니다.")) return; b.disabled = true; try { await act(b.dataset.apprNo, "reject"); } catch (e) { alert(e.message); b.disabled = false; } }));
  }

  // 부서 하나 = 방 하나. 책상 수에 따라 한 줄에 놓는 책상 수(열)가 늘어난다.
  const columnsFor = (desks) => (desks === 1 ? 1 : desks <= 6 ? 2 : desks <= 12 ? 3 : 4);

  function buildRoom(dept) {
    const seated = Array.from({ length: dept.desks }, (_, i) => agentsById.has(office.assignments[`${dept.id}:${i}`])).filter(Boolean).length;
    const room = document.createElement("section");
    room.className = "room";
    const codeStep = Object.entries(office.assignments)
      .filter(([k]) => k.startsWith(`${dept.id}:`))
      .map(([, agentId]) => CODE_SUBSTEPS[agentId]).find(Boolean);
    room.innerHTML = `<div class="room-title"><span class="dname">${esc(dept.name)}</span><span class="dcount">${seated}/${dept.desks}석</span></div><div class="room-desks" style="--cols:${columnsFor(dept.desks)}"></div>`;
    const desks = $(".room-desks", room);
    for (let i = 0; i < dept.desks; i++) {
      const agent = agentsById.get(office.assignments[`${dept.id}:${i}`]);
      const seat = document.createElement("button");
      seat.className = "seat" + (agent ? " taken" : " vacant");   // .empty 는 안내 상자 스타일이라 쓰지 않는다
      seat.dataset.dept = dept.id;
      seat.dataset.desk = i;
      seat.title = agent ? `${agent.name} · ${agent.role} — 눌러서 대화하기` : `${dept.name} ${i + 1}번 책상 (빈 자리)`;
      seat.innerHTML = `<span class="seat-no">${i + 1}</span><span class="run-light" title="실행 상태"></span><span class="seat-desk"></span><span class="monitor"></span>`
        + (agent
          ? `<span class="plate"><span class="seat-name">${esc(staffName(agent))}</span><span class="seat-role">${esc(agent.role)}</span></span>`
          : `<span class="plate"><span class="seat-name">빈 자리</span></span>`);
      // 캐릭터는 책상 뒤에 앉는다: 책상·모니터가 캐릭터 아랫부분을 가린다
      if (agent) seat.prepend(Object.assign(avatarImg(agent.appearance, 84, { transparent: true }), { className: "person" }));
      // 자리는 고정이다. 캐릭터가 앉은 책상은 눌러서 바로 대화하기로 간다
      if (agent) seat.addEventListener("click", () => { location.hash = `#/chat/${agent.id}`; });
      desks.appendChild(seat);
    }
    if (codeStep) room.insertAdjacentHTML("beforeend", `<div class="room-code-badge" title="${esc(codeStep.detail)}">⚙ ${esc(codeStep.label)}</div>`);
    return room;
  }

  // 설비실: 회사에 놓인 컴퓨터. 쓰는 부서는 흐름 설정(시작 전 준비)과 직원 도구(회사 컴퓨터)에서 그대로 읽는다 — 짐작하지 않는다.
  function computerUsers(pc) {
    const out = [];
    for (const d of office.departments) {
      const prep = flowState?.config?.departments?.[d.id]?.repeat?.prepare;
      if (prep && prep.computer === pc.id) out.push(`${d.name} · 시작 전 준비`);
      const uses = Object.entries(office.assignments).some(([k, aid]) => k.startsWith(`${d.id}:`) && (agentsById.get(aid)?.tools || []).includes("office_computer"));
      if (uses) out.push(`${d.name} · 찾아 씀`);
    }
    return out;
  }

  function buildEquipmentRoom() {
    const room = document.createElement("section");
    room.className = "room equip-room";
    room.innerHTML = `<div class="room-title"><span class="dname">설비실</span><span class="dcount">컴퓨터 ${computers.length}대</span></div><div class="equip-list"></div>`;
    const list = $(".equip-list", room);
    for (const pc of computers) {
      const users = computerUsers(pc);
      const a = document.createElement("a");
      a.className = "equip";
      a.dataset.pc = pc.id;
      a.href = `#/libraries/${pc.id}`;
      a.title = `${pc.name} — ${COMPUTER_STATE_TEXT[pc.state] || ""} (눌러서 컴퓨터 화면 열기)`;
      a.innerHTML = `${computerSvg(pc.state, 76)}<span class="plate"><span class="seat-name">${esc(pc.name)}</span>
        <span class="seat-role"><b class="st st-${esc(pc.state)}">${esc(COMPUTER_STATE_TEXT[pc.state] || "")}</b><span class="cnt"> · 문서 ${pc.documents}</span></span></span>
        <span class="equip-open">저장된 기사 ${pc.documents}건 보기 →</span>
        ${users.length ? `<span class="equip-users">${users.map((u) => `<span>${esc(u)}</span>`).join("")}</span>` : `<span class="equip-users muted">아직 쓰는 부서 없음</span>`}`;
      list.appendChild(a);
    }
    return room;
  }

  function draw() {
    $("#oCount").textContent = `${summaryText(office.departments)} · 배치 ${seatedCount(office, agentsById)}/${totalDesks(office.departments)}`;
    const stage = $("#seatBody");
    stage.replaceChildren();
    // 위: 직원 방(흐름 순서대로 가로), 아래: 설비실(컴퓨터를 가로로). 설비실은 흐름의 차례가 아니라 직원 아래에 따로 둔다.
    const staff = document.createElement("div");
    staff.className = "stage-staff";
    stage.appendChild(staff);
    const deptById = new Map(office.departments.map((d) => [d.id, d]));
    // 흐름 설정(mode·반복출처)을 알면 부서를 순서·화살표·병렬 갈래로 그린다 — 이름을 보고 짐작하지 않는다(office_flow.js 와 같은 로직 재사용).
    const groups = flowState?.plan ? groupDepartments(planToDiagramDepts(flowState.plan)) : office.departments.map((d) => ({ type: "single", depts: [{ id: d.id }] }));
    groups.forEach((g, gi) => {
      if (gi > 0) {
        const label = HANDOFF_LABELS[gi - 1] || "결과 JSON";
        staff.insertAdjacentHTML("beforeend", `<div class="stage-arrow">→<span>${esc(label)}</span></div>`);
      }
      if (g.type === "branch" && g.depts.length > 1) {
        const wrap = document.createElement("div");
        wrap.className = "stage-branch";
        wrap.innerHTML = `<div class="stage-branch-label">⚙ 흐름팀(코드) — 후보를 나눠 동시에 보냅니다</div>`;
        const row = document.createElement("div");
        row.className = "stage-branch-row";
        for (const gd of g.depts) { const dept = deptById.get(gd.id); if (dept) row.appendChild(buildRoom(dept)); }
        wrap.appendChild(row);
        staff.appendChild(wrap);
      } else {
        const dept = deptById.get(g.depts[0].id);
        if (dept) staff.appendChild(buildRoom(dept));
      }
    });
    if (document.documentElement.hasAttribute("data-v3")) {   // 시안 v3: 흐름의 끝에 결과물(진단 보고서)을 보여 준다
      staff.insertAdjacentHTML("beforeend", `<div class="stage-arrow">→<span>진단 보고서</span></div><a class="stage-result" href="#/dashboard"><b>진단 보고서</b><span>열람실에서 보기 →</span></a>`);
    }
    if (computers.length) stage.appendChild(buildEquipmentRoom());
    if (document.documentElement.hasAttribute("data-v3")) {   // 시안 v3: 사무실 느낌의 화분
      const plant = (cls) => `<svg class="plant ${cls}" viewBox="0 0 60 92" aria-hidden="true"><g stroke="#1d3b24" stroke-width="1.2" stroke-linejoin="round">
        <path d="M30 56 C20 44 8 40 6 22 C20 24 30 34 30 56Z" fill="#46a455"/><path d="M30 56 C40 44 52 40 54 22 C40 24 30 34 30 56Z" fill="#3a8f49"/>
        <path d="M30 58 C24 40 22 22 30 4 C38 22 36 40 30 58Z" fill="#58b865"/><path d="M30 60 C14 56 6 52 2 42 C16 40 26 46 30 60Z" fill="#2f7d3b"/><path d="M30 60 C46 56 54 52 58 42 C44 40 34 46 30 60Z" fill="#46a455"/>
        <path d="M12 62 H48 L43 88 H17 Z" fill="#c9764a" stroke="#6a3a22"/><rect x="9" y="56" width="42" height="9" rx="2" fill="#d98a5e" stroke="#6a3a22"/></g></svg>`;
      stage.insertAdjacentHTML("beforeend", plant("pl1") + plant("pl2") + plant("pl3") + plant("pl4"));
      // 컴퓨터실 칸막이의 출입구: 화제 탐지부(기사를 읽는 직원) 바로 아래 벽을 비워 직원이 드나들 수 있게 한다
      const placeDoor = () => {
        const eq = stage.querySelector(".equip-room"), rm = stage.querySelector(".stage-staff > .room");
        if (!eq || !rm) return;
        const e = eq.getBoundingClientRect(), r = rm.getBoundingClientRect();
        if (!e.width) return;
        const l = Math.max(0, ((r.left - e.left) / e.width) * 100), w = (r.width / e.width) * 100;
        eq.style.setProperty("--door-l", `${l.toFixed(2)}%`);
        eq.style.setProperty("--door-r", `${(l + w).toFixed(2)}%`);
      };
      requestAnimationFrame(() => { placeDoor(); setTimeout(placeDoor, 250); });
      if (!window.__doorBound) { window.__doorBound = true; window.addEventListener("resize", () => document.querySelector("#seatBody") && requestAnimationFrame(() => { const st = document.querySelector("#seatBody"); const eq = st.querySelector(".equip-room"), rm = st.querySelector(".stage-staff > .room"); if (!eq || !rm) return; const e = eq.getBoundingClientRect(), r = rm.getBoundingClientRect(); const l = Math.max(0, ((r.left - e.left) / e.width) * 100); eq.style.setProperty("--door-l", `${l.toFixed(2)}%`); eq.style.setProperty("--door-r", `${(l + (r.width / e.width) * 100).toFixed(2)}%`); })); }
    }
  }

  await loadFlow();
  draw();
  drawRun();
  if (flowState?.running) pollRun(flowState.running);

  // ---------------------------------------------------------------- 최근 실행(흐름 설정 화면에서 옮겨옴 — 배치 화면에서 바로 보인다)
  let historyTimer = null;
  async function drawHistory() {
    if (!alive()) return;
    const r = await renderRecentFlows($("#officeHistory"), officeId, alive);
    if (r && r.running) historyTimer = setTimeout(() => alive() && drawHistory(), 3000);
  }
  drawHistory();
}
