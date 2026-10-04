// 회사 컴퓨터 — 회사에 놓고 직원들이 함께 쓰는 설비(캐릭터가 아니다). $, api, esc, app 은 app.js, computerSvg 는 computer_art.js 에서 온다.
//   #/libraries/{id}   컴퓨터 한 대: 보관 자료
// 컴퓨터의 도구는 직원 도구와 같은 도구 목록에서 고른다(도구 정의는 플랫폼에 하나). 직원 전용 검색 형식은 컴퓨터에 설치할 수 없다.

const DOC_STATE = { queued: ["색인 대기", "busy"], extracting: ["읽는 중", "busy"], embedding: ["색인 중", "busy"], ready: ["검색 가능", "ok"], failed: ["실패", "bad"],
  stored: ["보관만(색인 안 함)", ""] };

function libraryStatsText(lib) {
  const parts = [`문서 ${lib.documents}건`];
  if (lib.tickers.length) parts.push(`종목 ${lib.tickers.length}개`);
  if (lib.processing) parts.push(`색인 중 ${lib.processing}`);
  if (lib.active_jobs) parts.push(`작업 중 ${lib.active_jobs}`);
  return parts.join(" · ");
}

const pcTime = (iso) => (iso || "").slice(0, 16).replace("T", " ");
const pcKB = (n) => (n < 1024 * 1024 ? `${Math.round(n / 1024)}KB` : `${(n / 1024 / 1024).toFixed(1)}MB`);

// ---------------------------------------------------------------- 컴퓨터 한 대
async function renderLibraryDetail(libraryId) {
  let lib = await api(`/api/libraries/${libraryId}`);
  let tab = "docs";
  let timer = null;

  app.innerHTML = `
    <div class="page-head head-row">
      <div class="pc-head">
        <div id="pcArt"></div>
        <div>
          <a class="back" href="#/offices">← 사무실</a>
          <h1 id="libTitle"></h1>
          <p id="libInfo" class="muted"></p>
        </div>
      </div>
    </div>
    <section id="pcBody" class="flow-section"></section>
    <dialog id="docDialog" class="doc-dialog"></dialog>`;

  const alive = () => document.body.contains($("#pcBody"));
  const paintHead = () => {
    $("#pcArt").innerHTML = computerSvg(lib.state, 96);
    $("#libTitle").textContent = lib.name;
    $("#libInfo").textContent = `${COMPUTER_STATE_TEXT[lib.state] || ""} · ${libraryStatsText(lib)} · `
      + (lib.offices.length ? `놓인 회사: ${lib.offices.map((o) => o.name).join(", ")}` : "아직 어느 회사에도 놓지 않았습니다(회사 배치 화면의 '컴퓨터' 칸에서 놓습니다)");
  };
  const refreshLib = async () => { lib = await api(`/api/libraries/${libraryId}`); paintHead(); };
  paintHead();

  // 컴퓨터 삭제 버튼은 화면에서 뺐다(실수로 문서와 기록을 지우지 않도록). 서버의 삭제 API 는 그대로 있다.

  function show() {
    clearTimeout(timer);
    $("#pcBody").dataset.tab = tab;   // 탭마다 화면 맞춤 방식이 달라서 CSS 가 알 수 있게 표시한다
    drawDocs();
  }
  const again = (fn, ms) => { clearTimeout(timer); timer = setTimeout(() => alive() && fn(), ms); };

  // ---------------------------------------------------------------- 보관 자료
  let filter = "";
  async function drawDocs() {
    const box = $("#pcBody");
    let inv;
    try {
      [inv] = await Promise.all([api(`/api/libraries/${libraryId}/inventory`), refreshLib()]);
    } catch (err) { box.innerHTML = `<div class="form-error">${esc(err.message)}</div>`; return; }
    if (!alive() || tab !== "docs") return;
    const s = inv.summary;
    const f = filter.trim().toUpperCase();
    const rows = inv.documents.filter((d) => !f || (d.ticker || "").includes(f) || d.filename.toUpperCase().includes(f));
    const groups = new Map();
    for (const d of rows) { const k = d.ticker || "(종목 정보 없음)"; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(d); }
    const docRow = (d) => {
      const [label, cls] = DOC_STATE[d.status] || [d.status, ""];
      const prog = d.status === "embedding" && d.progress_total ? ` ${d.progress_done}/${d.progress_total}` : "";
      const qpos = d.queue_pos ? `<div class="muted small">${d.queue_pos === 1 ? "지금 처리 중" : `색인 줄 ${d.queue_pos}번째`}</div>` : "";
      return `<tr class="${d.search_excluded ? "excluded" : ""}">
        <td>${esc(d.doc_type || "-")}<div class="muted small">${esc(d.period || "")}</div></td>
        <td class="n">${esc(d.published_at || "")}${d.published_at || d.status !== "ready" ? "" : `<span class="pill bad" title="게재일을 몰라 기준일 검색에서 빠집니다">게재일 없음</span>`}</td>
        <td class="n small">${esc(d.accession || "-")}</td>
        <td><span class="pill ${cls}">${label}${prog}</span>${qpos}${d.error ? `<div class="doc-error">${esc(d.error)}</div>` : ""}</td>
        <td>${d.search_excluded ? `<span class="pill bad" title="${esc(d.excluded_reason || "")}">검색 제외</span><div class="muted small">${esc(d.excluded_reason || "")}</div>` : `<span class="muted small">포함</span>`}</td>
        <td class="small">${d.origin ? `${esc(d.origin.requested_by)}<div class="muted small">${esc(pcTime(d.origin.at))}</div>` : `<span class="muted">직접 올림</span>`}</td>
        <td class="small">${esc(d.filename)}<div class="muted small">${pcKB(d.size)} · 조각 ${d.chunks}</div></td>
        <td class="doc-acts">
          <button type="button" data-view="${d.id}">원문</button>
          ${d.chunks ? `<button type="button" data-chunks="${d.id}" title="이 문서가 어떤 조각으로 나뉘고 벡터가 만들어졌는지">조각 보기</button>` : ""}
          ${d.search_excluded ? `<button type="button" data-include="${d.id}">검색에 다시 넣기</button>` : `<button type="button" data-exclude="${d.id}">검색 제외</button>`}
          ${d.status === "failed" || d.status === "stored" ? `<button type="button" data-retry="${d.id}">색인</button>` : ""}
          <button type="button" class="row-del" data-del="${d.id}" title="삭제" aria-label="삭제">×</button>
        </td></tr>`;
    };
    // 색인 중에는 3초마다 다시 그린다 — 그때 표 안·화면의 스크롤 위치와 입력 중이던 칸을 그대로 둔다
    const wasWrap = box.querySelector(".tablewrap"), active = document.activeElement;
    const keep = { wrap: wasWrap ? wasWrap.scrollTop : 0, body: box.scrollTop, page: window.scrollY,
      focusId: active && box.contains(active) ? active.id : "", sel: active && "selectionStart" in active ? [active.selectionStart, active.selectionEnd] : null };
    box.innerHTML = `
      <div class="pc-summary">
        <div><b>${s.tickers}</b><span>종목</span></div><div><b>${s.documents}</b><span>문서</span></div>
        <div class="ok"><b>${s.ready}</b><span>검색 가능</span></div><div class="busy"><b>${s.processing}</b><span>색인 중</span></div>
        <div class="bad"><b>${s.failed}</b><span>실패</span></div><div><b>${s.excluded}</b><span>검색 제외</span></div>
        <div class="${s.no_date ? "bad" : ""}"><b>${s.no_date}</b><span>게재일 없음</span></div>
        <div><b>${s.stored}</b><span>보관만</span></div>
      </div>
      <div class="pc-index ${inv.index.installed ? "" : "off"}">
        <b>문서 색인</b>
        ${inv.index.installed
          ? `<span>모델 <code>${esc(inv.index.model)}</code> · 색인 장비 ${inv.index.device.remote
              ? `<b>${esc(inv.index.device.remote.replace(/^https?:\/\//, ""))}</b>${inv.index.device.state === "error" ? ` <span class="pill bad" title="${esc(inv.index.device.message)}">응답 없음 — 색인 멈춤([이 PC]로 바꿀 수 있음)</span>` : inv.index.device.state === "ok" ? ` <span class="pill ok">연결됨</span>` : ""}`
              : "이 PC(CPU)"} · 검색 가능한 조각 ${inv.index.chunks.toLocaleString()}개${inv.index.queue_mine ? ` · 이 컴퓨터 문서 ${inv.index.queue_mine}건이 색인 줄에 있음(전체 줄 ${inv.index.queue_total}건 — 이 PC의 모든 컴퓨터·직원 문서함이 함께 씀)` : ""}</span>
             ${inv.index.stale ? `<span class="pill bad">모델이 바뀌어 전체 다시 색인 필요</span>` : ""}
             <span class="dev-switch" role="group" aria-label="색인 장비">
               <button type="button" data-dev="jetson" class="${inv.index.device.local ? "" : "on"}"${inv.index.device.saved ? "" : " disabled"} title="젯슨(${esc((inv.index.device.saved || "주소 없음").replace(/^https?:\/\//, ""))})으로 계산합니다">젯슨</button>
               <button type="button" data-dev="local" class="${inv.index.device.local ? "on" : ""}" title="이 PC(CPU)로 계산합니다. 처음 쓸 때 모델(약 2GB)을 내려받습니다">이 PC</button>
             </span>
             <button type="button" id="devEdit" class="link" title="젯슨 색인 장비의 주소를 바꿉니다">주소 변경</button>
             <span class="spacer"></span>
             <button type="button" id="idxNew" title="보관만 했거나 실패한 문서를 색인 줄에 넣습니다">색인 실행(새 문서만)</button>
             <button type="button" id="idxAll" title="색인을 지우고 모든 문서를 처음부터 다시 색인합니다">전체 다시 색인</button>`
          : `<span>이 컴퓨터에는 '문서 색인' 도구가 없어 받은 문서를 <b>보관만</b> 합니다. 직원이 찾으려면 색인이 필요합니다.</span><span class="spacer"></span>
             <button type="button" id="idxInstall">문서 색인 도구 설치</button>`}
      </div>
      <div class="lib-add">
        <input type="text" id="docFilter" placeholder="종목·파일 이름으로 거르기" value="${esc(filter)}" maxlength="40" />
        <button type="button" id="docClear" class="danger" title="이 컴퓨터에 보관된 문서를 모두 지웁니다">모두 지우기</button>
        <span class="form-error" id="docErr" role="alert"></span>
      </div>
      ${rows.length ? `<div class="tablewrap"><table class="jobs inv">
        <thead><tr><th>서식·기간</th><th>게재일</th><th>접수번호</th><th>색인</th><th>검색</th><th>받아 온 곳</th><th>파일</th><th></th></tr></thead>
        <tbody>${[...groups.entries()].map(([k, ds]) => `<tr class="group"><td colspan="8"><b>${esc(k)}</b> <span class="muted small">${ds.length}건</span></td></tr>${ds.map(docRow).join("")}`).join("")}</tbody>
      </table></div>` : `<div class="empty">${inv.documents.length ? "거르기에 맞는 문서가 없습니다." : "아직 보관한 자료가 없습니다. '설치된 도구'에서 도구를 실행하거나 파일을 올리세요."}</div>`}`;
    const runIdx = async (target) => {
      try {
        await api(`/api/libraries/${libraryId}/jobs`, { method: "POST", body: { tool: "rag_index", args: [{ target }] } });
        await refreshLib(); drawDocs();
      } catch (err) { $("#docErr").textContent = err.message; }
    };
    document.querySelectorAll(".dev-switch [data-dev]").forEach((b) => b.addEventListener("click", async () => {
      const local = b.dataset.dev === "local";
      if (b.classList.contains("on")) return;
      if (local && !confirm("이 PC(CPU)로 색인·검색 계산을 돌릴까요? 처음 한 번은 모델(약 2GB)을 내려받아 시간이 걸리고, 젯슨보다 느립니다. 젯슨 주소는 그대로 저장되어 있어 언제든 되돌릴 수 있습니다.")) return;
      try { await api("/api/index-device/mode", { method: "PUT", body: { local } }); await refreshLib(); drawDocs(); }
      catch (err) { $("#docErr").textContent = err.message; }
    }));
    // 젯슨 주소 바꾸기: 저장하기 전에 그 주소로 한 번 물어보고, 응답이 없으면 그대로 둘지 묻는다.
    if ($("#devEdit")) $("#devEdit").addEventListener("click", async () => {
      const now = inv.index.device.saved || "http://192.168.0.17:8790";
      const url = prompt("젯슨 색인 장비 주소를 적어 주세요 (예: http://192.168.0.17:8790)", now);
      if (url === null) return;
      const err = $("#docErr");
      err.textContent = "장비에 연결해 보는 중…";
      try {
        const t = await api("/api/index-device/test", { method: "POST", body: { url } });
        if (!t.ok && !confirm("그 주소에서 응답이 없습니다: " + (t.problem || "") + " 그래도 이 주소로 저장할까요?")) { err.textContent = ""; return; }
        await api("/api/index-device", { method: "PUT", body: { url } });
        err.textContent = "";
        await refreshLib(); drawDocs();
      } catch (e2) { err.textContent = e2.message; }
    });
    if ($("#idxNew")) $("#idxNew").addEventListener("click", () => runIdx("새 문서만"));
    if ($("#idxAll")) $("#idxAll").addEventListener("click", () => { if (confirm("색인을 지우고 모든 문서를 처음부터 다시 색인할까요? 문서가 많으면 몇 시간 걸리고, 그동안 직원이 이 컴퓨터를 찾을 수 없습니다.")) runIdx("전체 다시"); });
    if ($("#idxInstall")) $("#idxInstall").addEventListener("click", async () => {
      try {
        lib = await api(`/api/libraries/${libraryId}`, { method: "PUT", body: { name: lib.name, description: lib.description, tools: [...lib.tools, "rag_index"] } });
        paintHead(); drawDocs();
      } catch (err) { $("#docErr").textContent = err.message; }
    });
    const nowWrap = box.querySelector(".tablewrap");
    if (nowWrap) nowWrap.scrollTop = keep.wrap;
    box.scrollTop = keep.body;
    if (window.scrollY !== keep.page) window.scrollTo(0, keep.page);
    if (keep.focusId) {
      const el = document.getElementById(keep.focusId);
      if (el) { el.focus({ preventScroll: true }); if (keep.sel && el.setSelectionRange) el.setSelectionRange(keep.sel[0], keep.sel[1]); }
    }
    const fi = $("#docFilter");
    fi.addEventListener("input", () => { filter = fi.value; drawDocs().then(() => { const x = $("#docFilter"); if (x) { x.focus(); x.setSelectionRange(x.value.length, x.value.length); } }); });
    $("#docClear").addEventListener("click", async () => {   // 보도자료(문서) 모두 지우기 — 되돌릴 수 없어서 두 번 묻는다
      const n = lib.documents;
      if (!n) { alert("지울 문서가 없습니다."); return; }
      if (!confirm(`'${lib.name}'에 보관된 문서 ${n}건을 모두 지울까요?
직원들이 더는 찾을 수 없고 되돌릴 수 없습니다.`)) return;
      if (prompt("정말 지우려면 '삭제'라고 입력하세요.") !== "삭제") return;
      try { await api(`/api/libraries/${libraryId}/documents`, { method: "DELETE" }); await refreshLib(); drawDocs(); } catch (err) { alert(err.message); }
    });
    if (s.processing || inv.index.queue_mine) again(drawDocs, 3000);
  }

  $("#pcBody").addEventListener("click", async (e) => {
    const t = e.target.closest("button");
    if (!t || tab !== "docs") return;
    const base = `/api/libraries/${libraryId}/documents`;
    try {
      if (t.dataset.view) return openDoc(Number(t.dataset.view), 0);
      if (t.dataset.chunks) return openChunks(Number(t.dataset.chunks), 0);
      if (t.dataset.exclude) {
        const reason = prompt("검색에서 뺄 사유를 적어 주세요(문서는 지우지 않고 보관합니다):");
        if (!reason) return;
        await api(`${base}/${t.dataset.exclude}/exclude`, { method: "POST", body: { reason } });
      } else if (t.dataset.include) {
        await api(`${base}/${t.dataset.include}/include`, { method: "POST" });
      } else if (t.dataset.retry) {
        await api(`${base}/${t.dataset.retry}/retry`, { method: "POST" });
      } else if (t.dataset.del) {
        if (!confirm("이 문서를 컴퓨터에서 삭제할까요? 직원들이 더는 찾을 수 없고 되돌릴 수 없습니다.")) return;
        await api(`${base}/${t.dataset.del}`, { method: "DELETE" });
      } else return;
      await refreshLib(); drawDocs();
    } catch (err) { const x = $("#docErr"); if (x) x.textContent = err.message; }
  });

  async function openDoc(docId, start) {
    const dlg = $("#docDialog");
    const r = await api(`/api/libraries/${libraryId}/documents/${docId}/text?start=${start}`);
    const end = r.start + r.text.length;
    dlg.innerHTML = `<div class="doc-dialog-head"><b>${esc(r.filename)}</b><span class="muted small">${r.start + 1}–${end} / 전체 ${r.total}자</span>
        <span class="spacer"></span>${r.start > 0 ? `<button type="button" data-page="${Math.max(0, r.start - 20000)}">← 앞</button>` : ""}
        ${end < r.total ? `<button type="button" data-page="${end}">다음 →</button>` : ""}<button type="button" data-close>닫기</button></div>
      <pre class="doc-text">${esc(r.text)}</pre>`;
    dlg.querySelectorAll("[data-page]").forEach((b) => b.addEventListener("click", () => openDoc(docId, Number(b.dataset.page))));
    $("[data-close]", dlg).addEventListener("click", () => dlg.close());
    if (!dlg.open) dlg.showModal();
    $(".doc-text", dlg).scrollTop = 0;
  }

  async function openChunks(docId, start) {
    const dlg = $("#docDialog");
    const r = await api(`/api/libraries/${libraryId}/documents/${docId}/chunks?start=${start}&size=50`);
    const end = r.start + r.chunks.length;
    const withVec = r.chunks.filter((c) => c.vector).length;
    dlg.innerHTML = `<div class="doc-dialog-head"><b>${esc(r.filename)} — 색인 결과</b>
        <span class="muted small">조각 ${r.total}개 중 ${r.start + 1}–${end} · 이 쪽 벡터 ${withVec}/${r.chunks.length}${r.vector_dims ? ` · 벡터 길이 ${r.vector_dims}` : ""}</span>
        <span class="spacer"></span>${r.start > 0 ? `<button type="button" data-cpage="${Math.max(0, r.start - 50)}">← 앞</button>` : ""}
        ${end < r.total ? `<button type="button" data-cpage="${end}">다음 →</button>` : ""}<button type="button" data-close>닫기</button></div>
      <div class="chunk-list">${r.chunks.map((c) => `<div class="chunk"><div class="chunk-head"><b>#${c.n}</b>${c.page ? ` · ${c.page}쪽` : ""} · ${c.chars}자
          ${c.vector ? `<span class="pill ok">벡터 있음</span>` : `<span class="pill ${r.status === "ready" ? "bad" : "busy"}">벡터 없음</span>`}</div>
          <div class="chunk-text">${esc(c.text)}</div></div>`).join("") || `<div class="muted small">아직 조각이 없습니다(색인 전).</div>`}</div>`;
    dlg.querySelectorAll("[data-cpage]").forEach((b) => b.addEventListener("click", () => openChunks(docId, Number(b.dataset.cpage))));
    $("[data-close]", dlg).addEventListener("click", () => dlg.close());
    if (!dlg.open) dlg.showModal();
    $(".chunk-list", dlg).scrollTop = 0;
  }

  show();
}
