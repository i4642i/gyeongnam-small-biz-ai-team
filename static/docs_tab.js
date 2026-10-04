// 직원 소개의 '전문자료' 탭(읽기 전용). $, api, esc 는 app.js 에서 온다.
//   createDocsPanel(root, { agentId })  → { destroy() }
// 직원이 일할 때 찾아보는 서류 목록을 보여 주고, 서류를 누르면 안의 글을 팝업으로 연다.

const STATUS_TEXT = { queued: "대기 중", extracting: "글을 읽는 중", embedding: "분석 중", ready: "사용 가능", failed: "실패" };

function fmtSize(n) {
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)}KB`;
  return `${(n / 1024 / 1024).toFixed(1)}MB`;
}

function createDocsPanel(root, { agentId } = {}) {
  const base = `/api/agents/${agentId}`;
  let data = null;                  // 마지막으로 받은 문서 목록
  let timer = null;
  let dead = false;
  const notes = [];                 // 글 열기 실패 등 화면에 남겨 둘 안내 (닫기 전까지)

  root.innerHTML = `
    <div id="dpNotes"></div>
    <div id="dpList" class="doc-list"></div>`;

  const $r = (s) => $(s, root);

  // 서류를 누르면 안의 글을 팝업으로 보여 준다(긴 서류는 앞·다음으로 넘긴다)
  async function openText(docId, start) {
    let dlg = document.getElementById("docTextDialog");
    if (!dlg) { dlg = document.createElement("dialog"); dlg.id = "docTextDialog"; dlg.className = "doc-dialog"; document.body.appendChild(dlg); }
    let r;
    try { r = await api(`${base}/documents/${docId}/text?start=${start}`); } catch (err) { notes.push(err.message); renderNotes(); return; }
    const end = r.start + r.text.length;
    dlg.innerHTML = `<div class="doc-dialog-head"><b>${esc(r.filename)}</b><span class="muted small">${r.start + 1}–${end} / 전체 ${r.total}자</span>
        <span class="spacer"></span>${r.start > 0 ? `<button type="button" data-page="${Math.max(0, r.start - 20000)}">← 앞</button>` : ""}
        ${end < r.total ? `<button type="button" data-page="${end}">다음 →</button>` : ""}<button type="button" data-close>닫기</button></div>
      <pre class="doc-text">${esc(r.text)}</pre>`;
    dlg.querySelectorAll("[data-page]").forEach((b) => b.addEventListener("click", () => openText(docId, Number(b.dataset.page))));
    dlg.querySelector("[data-close]").addEventListener("click", () => dlg.close());
    if (!dlg.open) dlg.showModal();
    dlg.querySelector(".doc-text").scrollTop = 0;
  }

  function renderNotes() {
    $r("#dpNotes").innerHTML = notes.map((n, i) =>
      `<div class="notice warn note-row"><span>${esc(n)}</span><button type="button" class="link" data-note="${i}" aria-label="닫기">×</button></div>`).join("");
  }
  $r("#dpNotes").addEventListener("click", (e) => {
    const b = e.target.closest("[data-note]");
    if (b) { notes.splice(Number(b.dataset.note), 1); renderNotes(); }
  });

  // ---------------------------------------------------------------- 목록
  function statusPill(d) {
    if (d.status === "embedding" && d.progress_total) return `<span class="pill busy">분석 중 ${d.progress_done}/${d.progress_total}</span>`;
    const cls = d.status === "ready" ? "ok" : d.status === "failed" ? "bad" : "busy";
    return `<span class="pill ${cls}">${STATUS_TEXT[d.status] ?? d.status}</span>`;
  }

  function renderList() {
    const el = $r("#dpList");
    if (!data) { el.innerHTML = `<div class="muted small">문서 목록을 불러오는 중…</div>`; return; }
    const rows = data.documents.map((d) => {
      const meta = [fmtSize(d.size)];
      if (d.status === "ready" || d.status === "embedding") {
        if (d.pages > 1 || d.ext === ".pdf") meta.push(`${d.pages}쪽`);
        meta.push(`조각 ${d.chunks}개`);
        if (d.empty_pages) meta.push(`글 없는 쪽 ${d.empty_pages}`);
      }
      return `<div class="doc-row ${d.status}">
        <div class="doc-main" data-open="${d.id}" role="button" tabindex="0" title="눌러서 내용 보기"><b title="${esc(d.filename)}">${esc(d.filename)}</b><span class="muted small">${meta.join(" · ")}</span>
          ${d.error ? `<span class="doc-error">${esc(d.error)}</span>` : ""}</div>
        ${statusPill(d)}</div>`;
    });
    const s = data.summary;
    el.innerHTML = (data.documents.length
      ? `<div class="doc-summary muted small">문서 ${s.total}개 · 사용 가능 ${s.ready}${s.processing ? ` · 처리 중 ${s.processing}` : ""}${s.failed ? ` · 실패 ${s.failed}` : ""}</div>`
      : "") + (rows.length ? rows.join("") : `<div class="muted small doc-empty">아직 들어 있는 서류가 없습니다.</div>`);
  }

  root.addEventListener("click", (e) => {
    const opener = e.target.closest("[data-open]");
    if (opener) openText(Number(opener.dataset.open), 0);
  });
  root.addEventListener("keydown", (e) => {
    const opener = e.target.closest("[data-open]");
    if (opener && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openText(Number(opener.dataset.open), 0); }
  });

  // ---------------------------------------------------------------- 새로고침(처리 중에는 계속 확인)
  async function refresh() {
    if (dead) return;
    try {
      data = await api(`${base}/documents`);
    } catch (err) {
      if (!data) $r("#dpList").innerHTML = `<div class="form-error">${esc(err.message)}</div>`;
      return schedule(4000);
    }
    if (dead) return;
    renderList();
    schedule(data.documents.some((d) => ["queued", "extracting", "embedding"].includes(d.status)) ? 1200 : 0);
  }
  function schedule(ms) {
    clearTimeout(timer);
    if (ms && !dead) timer = setTimeout(refresh, ms);
  }

  renderList();
  refresh();
  // 화면을 떠나면(다른 라우트로 이동해 root 가 문서에서 빠지면) 확인을 멈춘다
  const watcher = setInterval(() => { if (!root.isConnected) destroy(); }, 2000);
  function destroy() { dead = true; clearTimeout(timer); clearInterval(watcher); }

  return { destroy };
}
