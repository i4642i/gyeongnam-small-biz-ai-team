// 회사 흐름 화면 (R5 1단계):
//   #/offices/{id}/flows/{flowId}  한 번의 실행 기록(부서별 상태, 책상별 결과, 합계)
// $, api, esc, app, avatarImg 는 app.js 에서, byId 는 offices.js 에서 온다.

const FLOW_STATUS = {
  pending: ["대기", ""], running: ["진행 중", "busy"], ok: ["완료", "ok"], hold: ["보류", "warn"], failed: ["실패", "bad"], skipped: ["건너뜀", ""],
  error: ["오류", "bad"], interrupted: ["중단됨", "bad"], paused: ["승인 대기", "busy"], awaiting_approval: ["승인 대기", "busy"], stopped: ["정지됨", "bad"],
};
const FLOW_RUN_STATUS = { running: ["실행 중", "busy"], ok: ["성공", "ok"], failed: ["멈춤(실패)", "bad"], error: ["오류", "bad"], interrupted: ["중단됨", "bad"], stopped: ["정지됨", "bad"] };
const flowTime = (iso) => { try { return new Date(iso).toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }); } catch { return iso || ""; } };
const flowPill = (map, s) => { const [label, cls] = map[s] || [s, ""]; return `<span class="pill ${cls}">${esc(label)}</span>`; };
const flowInputs = (inputs) => Object.entries(inputs || {}).filter(([, v]) => v !== "").map(([k, v]) => `${k}=${v}`).join(", ");
// 최근 실행 목록용: 개발자식 key=value 대신 "창원시 · 9/17~10/1", 초 대신 "6분 57초"
const flowInputsNice = (inp) => {
  const i = inp || {};
  const md = (d) => { const m = /^\d{4}-(\d{2})-(\d{2})/.exec(String(d || "")); return m ? `${+m[1]}/${+m[2]}` : ""; };
  const parts = [i.region, i.area, i.upjong].filter(Boolean);
  const range = i.window_start && i.window_end ? `${md(i.window_start)}~${md(i.window_end)}` : "";
  return [parts.join(" "), range].filter(Boolean).join(" · ");
};
const flowDur = (s) => { const n = Math.round(Number(s)); if (!isFinite(n)) return "…"; const m = Math.floor(n / 60); return m ? `${m}분 ${n % 60}초` : `${n}초`; };
const fmtNum = (n) => (n || 0).toLocaleString("ko-KR");
const totalsText = (t) => (t ? `모델 호출 ${fmtNum(t.model_calls)}회 · 입력 ${fmtNum(t.input_tokens)} · 출력 ${fmtNum(t.output_tokens)} · 캐시 읽기 ${fmtNum(t.cache_read_tokens)} · 도구 ${fmtNum(t.tool_calls)}회${t.warnings ? ` · 경고 통과 ${fmtNum(t.warnings)}건` : ""}` : "");

// ---------------------------------------------------------------- 흐름 그림(R8): 부서 배치를 화살표로 · 설정값 반영 · 실행 상태
// 이 회사(o_9f89d063)의 실제 배선을 그대로 옮긴 이름표다 — 부서가 바뀌면 이 라벨은 안 맞을 수 있으니 개수만큼만 쓰고
// 모자라면 일반 이름으로 대신한다(부서 자체·상태·설정 영향은 전부 실제 데이터에서 읽는다, 여기서 지어내지 않는다).
const HANDOFF_LABELS = ["기자 보고서", "후보 목록", "검증 결과 · 동종비교 결과", "포트폴리오"];
// 입력 이름(공통 설정)의 뜻 한 줄 — app/offices/flow.py 가 이름만으로 공통 입력을 묶으므로, 알려진 이름만 설명을 달고
// 모르는 이름은 설명 없이(추측하지 않고) 이름만 보여준다.
const FLOW_INPUT_MEANING = {
  window_start: "관측 시작일",
  window_end: "관측 종료일(비워두면 오늘)",
  run_type: "실행 주기 — weekly(주간 정기)·daily(일간)·event(사건 발생 시 임시)",
  gov_config: "정책 애널리스트 전용 — 의회 구도 가정: unified(단점 정부) · divided(분점 정부)",
  test_mode: "리서치 팀장 전용 — 켜면 점수 계산을 시험(비용 절감) 모드로 돌립니다",
};
// 이 캐릭터의 업무는 모델이 아니라 도구(코드)가 값을 정하는 단계가 있다는 걸 안다(이번 세션에 직접 확인한 사실,
// 부서 이름만으로는 알 수 없어 여기 정리해 둔다) — 새 캐릭터가 생기면 이 표에 없을 뿐 흐름 자체는 그대로 그려진다.
const CODE_SUBSTEPS = {
  a_8d3d7a89: { label: "비중 계산(코드)", detail: "pm_build_portfolio — 확신도·검증 판정으로 편입 비중을 규칙대로 계산합니다(모델이 정하지 않습니다)." },
};

function daysBetween(a, b) {
  const da = new Date(a), db = new Date(b);
  if (Number.isNaN(+da) || Number.isNaN(+db)) return null;
  return Math.round((db - da) / 86400000);
}

// 부서 목록을 노드 그룹으로 나눈다: 같은 반복 출처(repeat.source)를 공유하는 연속 부서는 나란히(병렬 갈래) 그린다.
// 이름을 보고 짐작하지 않고 mode·repeatSource(둘 다 회사 설정에서 그대로 옴)만 본다.
function groupDepartments(departments) {
  const groups = [];
  let i = 0;
  while (i < departments.length) {
    const d = departments[i];
    if (d.mode === "repeat" && d.repeatSource) {
      const branch = [d];
      let j = i + 1;
      while (j < departments.length && departments[j].mode === "repeat" && departments[j].repeatSource === d.repeatSource) { branch.push(departments[j]); j++; }
      groups.push({ type: "branch", depts: branch });
      i = j;
    } else {
      groups.push({ type: "single", depts: [d] });
      i++;
    }
  }
  return groups;
}

// state.plan.departments(설정 화면) → 다이어그램용 모양으로 바꾼다(상태·소요시간 없음).
function planToDiagramDepts(plan) {
  return plan.departments.map((d) => ({
    id: d.id, name: d.name, mode: d.mode, modeLabel: d.mode === "repeat" ? "반복" : d.mode === "single" ? "단일" : "동시",
    repeatSource: d.repeat ? d.repeat.source : null, prepare: d.repeat ? d.repeat.prepare || null : null,
    desks: d.desks.map((x) => ({ key: x.key, name: x.agent.name, role: x.agent.role, agentId: x.agent.id })),
  }));
}

// ---------------------------------------------------------------- 최근 실행(흐름 설정 화면·배치 화면이 같이 쓴다)
async function renderRecentFlows(box, officeId, alive = () => true) {
  let r;
  try { r = await api(`/api/offices/${officeId}/flows`); } catch { return null; }
  if (!alive()) return null;
  if (!r.flows.length) { box.innerHTML = `<span class="muted small">아직 실행한 기록이 없습니다.</span>`; return r; }
  box.innerHTML = `<p class="muted small">줄을 누르면 실행 상세(부서·책상 상태, "이 책상만 다시" 포함)가 열립니다.</p>
    <table class="flow-table"><thead><tr><th>시작</th><th>상태</th><th>소요</th><th>입력</th><th></th></tr></thead><tbody>${r.flows.map((f) => `
    <tr class="flow-row-link" data-flow="${f.id}" tabindex="0" role="link"><td><a href="#/offices/${officeId}/flows/${f.id}">${esc(flowTime(f.started_at))} →</a></td><td>${flowPill(FLOW_RUN_STATUS, f.status)}${f.status === "failed" && f.stopped_at ? ` <span class="muted small">${esc(f.stopped_at.department)}</span>` : ""}</td>
      <td>${f.duration_s != null ? flowDur(f.duration_s) : "…"}</td><td>${esc(flowInputsNice(f.inputs) || flowInputs(f.inputs))}</td>
      <td>${f.status === "running" ? "" : `<button class="danger" data-del="${f.id}">삭제</button>`}</td></tr>`).join("")}</tbody></table>`;
  box.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (!confirm("이 실행 기록을 지울까요?\n(각 책상의 업무 실행 기록은 그대로 남습니다.)")) return;
    try { await api(`/api/offices/${officeId}/flows/${b.dataset.del}`, { method: "DELETE" }); renderRecentFlows(box, officeId, alive); } catch (err) { alert(err.message); }
  }));
  box.querySelectorAll("tr[data-flow]").forEach((tr) => {
    const go = () => { location.hash = `#/offices/${officeId}/flows/${tr.dataset.flow}`; };
    tr.addEventListener("click", (e) => { if (e.target.closest("[data-del]") || e.target.closest("a")) return; go(); });
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  });
  return r;
}

// ---------------------------------------------------------------- 실행 기록 (한 번의 전체 흐름)

async function renderFlowRun(officeId, flowId) {
  let timer = null;
  app.classList.add("wide");
  app.innerHTML = `
    <div class="page-head">
      <a class="back" href="#/offices/${officeId}">← 사무실로</a>
      <h1>회사 실행 기록</h1>
      <p id="fHead" class="muted" hidden></p>
    </div>
    <section class="flow-section" hidden><h3>이 실행의 공통 입력</h3>
      <div id="fSettings" class="flow-settings"></div>
    </section>
    <div id="fBody"></div>`;

  function connTable(f, nextDept) {
    // nextDept 의 책상들이 받는 입력 중 출처가 있는 것 전부(바로 앞 부서만이 아니라 건너뛰기 연결도 포함)
    const rows = [];
    for (const x of nextDept.desks) {
      for (const [name, key] of Object.entries(x.sources || {})) {
        const conn = x.input_conn?.[name] || {};
        rows.push({ into: `${nextDept.name} · ${x.agent_name}`, name, from: depName(f, key), mode: conn.mode_label || "JSON 전체", chars: x.input_chars?.[name] });
      }
    }
    if (!rows.length) return `<div class="muted small">이 부서로 넘어가는 결과 연결이 없습니다(회사 공통 입력만 씁니다).</div>`;
    return `<table class="flow-conn-table"><thead><tr><th>보내는 쪽</th><th>입력 항목</th><th>받는 쪽</th><th>넘길 내용</th><th>분량</th></tr></thead><tbody>${rows.map((r) => `<tr><td>${esc(r.from)}</td><td class="muted small">${esc(r.name)}</td><td>${esc(r.into)}</td><td class="muted small">${esc(r.mode)}</td><td class="muted small">${r.chars ? fmtNum(r.chars) + "자(약 " + fmtNum(Math.round(r.chars / 3.4)) + "토큰)" : "-"}</td></tr>`).join("")}</tbody></table>`;
  }

  function drawFSettings(f) {
    const names = Object.keys(f.inputs || {});
    if (!names.length) { $("#fSettings").innerHTML = `<span class="muted small">이번 실행에 회사 공통 입력이 없습니다.</span>`; return; }
    $("#fSettings").innerHTML = names.map((name) => {
      const meaning = FLOW_INPUT_MEANING[name] || "";
      let extra = "";
      if (name === "window_start" || name === "window_end") {
        const days = daysBetween(f.inputs.window_start, f.inputs.window_end);
        if (days != null) extra = `관측 기간 ${days}일`;
      }
      return `<div class="flow-setting"><label>${esc(name)}</label><div>${esc(f.inputs[name])}</div>
        ${meaning ? `<span class="muted small">${esc(meaning)}</span>` : ""}${extra ? `<span class="muted small">${esc(extra)}</span>` : ""}</div>`;
    }).join("");
  }

  async function draw() {
    let f;
    try { f = await api(`/api/offices/${officeId}/flows/${flowId}`); } catch (err) { $("#fBody").innerHTML = `<div class="empty">${esc(err.message)}</div>`; return; }
    if (!document.body.contains($("#fBody"))) return;
    drawFSettings(f);
    $("#fHead").innerHTML = `${esc(f.office_name)} · ${flowPill(FLOW_RUN_STATUS, f.status)} · ${esc(flowTime(f.started_at))} 시작${f.duration_s != null ? ` · ${f.duration_s}초` : ""}
      ${f.inputs && Object.keys(f.inputs).length ? `<br>입력: ${esc(flowInputs(f.inputs))}` : ""}${f.totals ? `<br>${esc(totalsText(f.totals))}` : ""}
      ${f.resumes && f.resumes.length ? `<br><span class="muted small">다시 실행: ${f.resumes.map((r) => `${esc(r.scope === "desk" ? `${r.department} · ${r.desk_agent}` : r.department)}(${esc(flowTime(r.at))})`).join(", ")}</span>` : ""}`;
    const stop = f.status === "failed" && f.stopped_at ? `<div class="flow-note bad">⛔ <b>${esc(f.stopped_at.department)}</b> 에서 멈췄습니다. 검증을 통과하지 못한 결과는 다음 부서로 넘기지 않습니다. 아래 실패한 책상의 사유를 확인하고, 고친 뒤 그 부서의 「여기서부터 다시」를 누르세요.</div>` : "";
    const err = f.error ? `<div class="flow-note bad">${esc(f.error)}</div>` : "";
    const busy = f.status === "running";
    $("#fBody").innerHTML = stop + err + f.departments.map((d, di) => `
      <div class="flow-dept" data-st="${d.status}">
        <div class="flow-dept-head"><span class="flow-no">${di + 1}</span><b>${esc(d.name)}</b><span class="muted small">${d.mode === "single" ? "단일" : d.mode === "repeat" ? "반복" : "동시"}</span>${flowPill(FLOW_STATUS, d.status)}</div>
        <div class="flow-desks">${d.desks.map((x) => `
          <div class="flow-desk done-${x.status}">
            <div class="flow-who"><div><b>${esc(x.agent_name)}</b><div class="muted small">${esc(x.agent_role || "")}</div></div></div>
            <div class="flow-task"><div class="muted small">업무</div>${esc(x.task_name || "")}</div>
            <div class="flow-ins">
              <div>${flowPill(FLOW_STATUS, x.status)}${x.warnings && x.warnings.length ? ` <span class="pill warn" title="${esc(x.warnings.join("\n"))}">경고 ${x.warnings.length}건</span>` : ""} ${x.status === "running" || x.status === "paused" ? `<span class="muted small">${esc(x.stage || "")}</span>` : ""}${x.status === "paused" ? ` <a href="#/run/${x.agent_id}">업무 실행 기록에서 이어서 쓰기/중단</a>` : ""}${x.duration_s != null ? ` <span class="muted small">${x.duration_s}초${x.tool_calls != null ? ` · 도구 ${x.tool_calls}회` : ""}</span>` : ""}</div>
              ${x.error ? `<div class="flow-err">${esc(x.error)}</div>` : ""}
              ${Object.keys(x.sources || {}).length ? `<div class="muted small">받은 결과: ${Object.entries(x.sources).map(([n, k]) => `${esc(n)} ← ${esc(depName(f, k))}(${esc(x.input_conn?.[n]?.mode_label || "JSON 전체")})`).join(", ")}</div>` : ""}
              <div class="small">
                ${f.output_chars?.[x.key] ? ` · <a href="/api/offices/${officeId}/flows/${flowId}/desks/${encodeURIComponent(x.key)}/json" target="_blank" rel="noopener">결과 JSON(${fmtNum(f.output_chars[x.key])}자)</a>` : ""}</div>
              ${x.status === "ok" && x.run_id ? `<div class="flow-dl">
                <a class="btn small" href="/api/agents/${x.agent_id}/runs/${x.run_id}/readable" target="_blank" rel="noopener" title="사람이 읽기 좋게 바꾼 보고서(새 탭) — 모델을 부르지 않고 규칙대로 변환">📖 읽기 전용 보고서</a></div>` : ""}
            </div>
          </div>`).join("")}</div>
      </div>${di < f.departments.length - 1 ? `<details class="flow-arrow-conn" id="conn-${esc(f.departments[di + 1].id)}"><summary class="flow-arrow" aria-label="이 사이의 결과→입력 연결 보기">↓ 결과 전달 (눌러서 보기)</summary>${connTable(f, f.departments[di + 1])}</details>` : ""}`).join("");
    if (f.status === "running") timer = setTimeout(draw, 2000);
  }
  const depName = (f, key) => { for (const d of f.departments) for (const x of d.desks) if (x.key === key) return `${d.name} · ${x.agent_name}`; return key; };
  await draw();
}
