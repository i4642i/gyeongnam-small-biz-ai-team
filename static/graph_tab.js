// 캐릭터의 '지식 그래프' 탭. $, api, esc 는 app.js 에서 온다.
//   createGraphPanel(root, { agentId })  → { destroy() }
// 문서 조각에서 뽑은 개체(노드)와 관계(간선)를 캔버스에 그린다. 외부 라이브러리 없이 직접 만든 힘 기반 배치를 쓴다.
//   - 드래그: 빈 곳 = 화면 이동, 노드 = 노드 이동 / 휠 = 확대·축소 / 클릭 = 개체 선택(오른쪽에 관계와 출처)
// 입력칸에는 name 속성을 달지 않는다(폼의 FormData 에 섞이지 않게).

const TYPE_COLORS = {
  "사람": "#e8590c", "조직": "#3563e9", "장소": "#2f9e5b", "제품": "#ae3ec9",
  "개념": "#0c8599", "규정": "#c92a2a", "수치·날짜": "#f08c00", "기타": "#868e96",
};
const GROUP_COLORS = ["#3563e9", "#e8590c", "#2f9e5b", "#ae3ec9", "#0c8599", "#c92a2a", "#f08c00", "#5c7cfa", "#20c997", "#e64980", "#868e96"];

function createGraphPanel(root, { agentId = null } = {}) {
  if (agentId === null) {
    root.innerHTML = `<div class="placeholder"><b>지식 그래프</b>
      <p>문서 속의 사람·조직·개념과 그 관계를 한눈에 보는 지도입니다.<br>캐릭터를 만든 뒤, 전문자료를 올리고 이 탭에서 만들 수 있습니다.</p></div>`;
    return { destroy() {} };
  }

  root.innerHTML = `
    <div class="graph-bar">
      <div id="gpStatus" class="graph-status"></div>
    </div>
    <div id="gpMsg"></div>
    <div class="graph-full" id="gpFull">
    <div class="graph-wrap">
      <div class="graph-main">
      <div class="graph-outline" id="gpOutline" hidden></div>
      <div class="graph-stage" id="gpStage">
        <canvas id="gpCanvas" aria-label="지식 그래프 지도"></canvas>
        <div class="graph-zoom"><button type="button" data-z="in" aria-label="확대">+</button><button type="button" data-z="out" aria-label="축소">−</button><button type="button" data-z="fit" aria-label="전체 보기">⤢</button><button type="button" data-z="full" id="gpFullBtn" aria-label="전체 화면" title="전체 화면 (Esc로 닫기)">⛶</button></div>
        <div id="gpEmpty" class="graph-empty"></div>
      </div>
      </div>
      <aside class="graph-side" id="gpSide"></aside>
    </div>
    <div id="gpLegend" class="graph-legend"></div>
    </div>`;

  const $r = (s) => $(s, root);
  const canvas = $r("#gpCanvas");
  const stage = $r("#gpStage");
  const ctx = canvas.getContext("2d");

  let data = null, status = null;
  let nodes = [], edges = [], byId = new Map(), adj = new Map();
  let tf = { k: 1, x: 0, y: 0 };
  let W = 0, H = 0, dpr = 1;
  let alpha = 0, raf = 0, dead = false, needFit = true, timer = null;
  let selected = null, hover = null, drag = null, matches = null;
  let userMoved = false, frames = 0, userPickedLayout = false;   // 사용자가 화면을 옮기기 전까지는 배치가 바뀔 때마다 전체가 보이게 다시 맞춘다
  let lastDone = -1, detailToken = 0, lastBoxes = [], lastAspect = 0;
  const opts = { color: "type", edgeLabels: false, limit: 150 };
  let vn = [], ve = [], rootDist = null, guides = null, heads = [], treeAnim = false, compCount = 1;   // 지금 화면에 보이는 개체·관계(깊이 제한 적용), 기준 개체로부터의 거리, 층 안내선
  const view = { layout: "free", depth: 0 };                                // 배치 방식(free/tree/radial), 기준 개체로부터 보여 줄 단계 수(0 = 전체)

  // ---------------------------------------------------------------- 안내·상태
  function renderStatus() {
    if (!status) return;
    $r("#gpStatus").innerHTML = `분석한 조각 <b>${status.done_chunks}/${status.total_chunks}</b> · 개체 <b>${status.entities}</b> · 관계 <b>${status.relations}</b>`;
  }

  function renderEmpty() {
    const el = $r("#gpEmpty");
    if (nodes.length) { el.innerHTML = ""; el.hidden = true; return; }
    el.hidden = false;
    el.innerHTML = !status?.total_chunks
      ? `<b>아직 분석할 문서가 없습니다</b>`
      : status.job.state === "running" ? `<b>만드는 중…</b><span>개체가 나오는 대로 여기에 나타납니다.</span>`
      : `<b>아직 지식 그래프가 없습니다</b>`;
  }

  // ---------------------------------------------------------------- 데이터 → 그래프
  function radiusOf(n, maxScore) { return 5 + 15 * Math.sqrt(n.score / (maxScore || 1)); }

  function applyData(d) {
    data = d;
    status = d.status;
    const old = new Map(nodes.map((n) => [n.id, n]));
    const maxScore = Math.max(0, ...d.nodes.map((n) => n.score));
    const groups = Math.max(1, ...d.nodes.map((n) => n.community + 1));
    nodes = d.nodes.map((n) => {
      const prev = old.get(n.id);
      const ang = (2 * Math.PI * n.community) / groups + (Math.random() - 0.5) * 0.8;
      const rad = 80 + Math.random() * 140;
      return { ...n, r: radiusOf(n, maxScore),
        x: prev ? prev.x : Math.cos(ang) * rad, y: prev ? prev.y : Math.sin(ang) * rad, vx: 0, vy: 0, fixed: false };
    });
    byId = new Map(nodes.map((n) => [n.id, n]));
    edges = d.edges.map((e) => ({ ...e, a: byId.get(e.source), b: byId.get(e.target) })).filter((e) => e.a && e.b);
    adj = new Map(nodes.map((n) => [n.id, new Set()]));
    for (const e of edges) { adj.get(e.source).add(e.target); adj.get(e.target).add(e.source); }
    if (selected && !byId.has(selected)) selected = null;
    const fresh = nodes.some((n) => !old.has(n.id));
    computeVisible();
    if (view.layout === "free") {
      for (let i = 0; i < (old.size ? 60 : 160); i++) tick(fresh ? 0.8 : 0.3);   // 처음에는 미리 여러 번 움직여 자리를 잡는다
      alpha = old.size ? 0.3 : 0.1;
    } else if (view.layout === "tree" || view.layout === "radial") layoutTree();
    if (!old.size) needFit = true;
    if (!userPickedLayout && !document.querySelector(".profile-mode") && view.layout === "free" && edges.some((e) => e.category)) { view.layout = "outline"; $r("#gpLayout").value = "outline"; }   // 분류가 있으면(사용자가 직접 고르기 전까지) 목록으로 보여 준다
    renderStatus(); renderEmpty(); renderLegend(); renderSide(); applyLayoutMode();
    if (needFit || !userMoved) fit();
    kick();
  }

  const LABEL_FONT = '13px system-ui, "Malgun Gothic", sans-serif';
  const measureCtx = document.createElement("canvas").getContext("2d");
  const measureLabel = (t) => { measureCtx.font = LABEL_FONT; return measureCtx.measureText(t).width; };

  // ---------------------------------------------------------------- 깊이·계층 보기
  // 지식 그래프는 위아래가 정해진 트리가 아니라 그물이다. 그래서 "계층"은 기준 개체에서 연결을 따라 몇 걸음 떨어졌는지(깊이)로 층을 나눈 것이다.
  // 관계의 방향(→)과는 무관하게 연결만 따라간다. 기준 개체는 선택한 개체, 선택이 없으면 가장 중요한 개체다.
  function rootId() {
    if (selected != null && byId.has(selected)) return selected;
    if (view.layout === "outline") return null;
    if (view.layout === "free" && view.depth === 0) return null;   // 자유 배치로 전체를 볼 때는 기준이 필요 없다
    return nodes.length ? nodes.reduce((a, b) => (b.score > a.score ? b : a)).id : null;
  }

  function bfs(root) {
    const dist = new Map([[root, 0]]);
    const q = [root];
    for (let i = 0; i < q.length; i++) for (const nb of adj.get(q[i]) || []) if (!dist.has(nb)) { dist.set(nb, dist.get(q[i]) + 1); q.push(nb); }
    return dist;
  }

  function computeVisible() {
    const root = rootId();
    rootDist = root != null ? bfs(root) : null;
    vn = view.layout !== "outline" && root != null && view.depth > 0 ? nodes.filter((n) => (rootDist.get(n.id) ?? Infinity) <= view.depth) : nodes;
    const set = new Set(vn.map((n) => n.id));
    ve = edges.filter((e) => set.has(e.source) && set.has(e.target));
  }

  function layoutTree() {
    const root = rootId();
    guides = []; heads = []; compCount = 1; lastAspect = W / Math.max(1, H);
    for (const n of nodes) { n.tx = undefined; n.ty = undefined; }
    if (root == null || !vn.length) return;
    const vis = new Set(vn.map((n) => n.id));
    const seen = new Set();

    // 연결된 덩어리마다, 그 덩어리의 대표 개체(기준 개체가 속한 덩어리는 기준 개체)에서 가까운 순으로 뻗어 나가는 나무(BFS 트리)를 만든다.
    // 한 개체는 처음 닿은 길 하나만 부모로 삼고, 나머지 연결선은 그냥 그린다.
    const grow = (start) => {
      const depth = new Map([[start, 0]]);
      const kids = new Map();
      const order = [start];
      seen.add(start);
      for (let i = 0; i < order.length; i++) {
        const id = order[i];
        kids.set(id, []);
        const next = [...(adj.get(id) || [])].filter((x) => vis.has(x) && !seen.has(x)).sort((a, b) => byId.get(b).score - byId.get(a).score);
        for (const x of next) { seen.add(x); depth.set(x, depth.get(id) + 1); kids.get(id).push(x); order.push(x); }
      }
      const leaves = new Map();
      for (let i = order.length - 1; i >= 0; i--) { const ks = kids.get(order[i]); leaves.set(order[i], ks.length ? ks.reduce((a, k) => a + leaves.get(k), 0) : 1); }
      const perLevel = [];
      for (const d of depth.values()) perLevel[d] = (perLevel[d] || 0) + 1;
      return { root: start, depth, kids, order, leaves, maxL: perLevel.length - 1, perLevel };
    };
    const comps = [grow(root)];
    for (const n of [...vn].sort((a, b) => b.score - a.score)) if (!seen.has(n.id)) comps.push(grow(n.id));
    const singles = comps.filter((c, i) => i > 0 && c.order.length === 1).map((c) => byId.get(c.root));   // 연결이 하나도 없는 개체
    const blocks = [comps[0], ...comps.slice(1).filter((c) => c.order.length > 1).sort((a, b) => b.order.length - a.order.length)];
    compCount = blocks.length;

    // 덩어리 하나의 안쪽 좌표(0,0에서 시작)와 크기를 정한다. 간격은 개체 이름의 글자 폭으로 잡아서 이름이 서로 겹치지 않게 한다.
    const lw = (n) => (n.lw ??= Math.max(44, measureLabel(n.name)) + 22);   // 이름 폭 + 여백
    const measure = (c) => {
      if (view.layout === "tree") {
        // 부분 트리마다 폭 = max(자기 이름 폭, 자식들 폭의 합). 자식들은 부모 아래에 나란히, 부모는 그 가운데에 놓인다.
        const GY = Math.max(66, Math.min(100, 900 / Math.max(1, c.maxL)));
        const width = new Map();
        for (let i = c.order.length - 1; i >= 0; i--) {
          const id = c.order[i], ks = c.kids.get(id);
          width.set(id, Math.max(lw(byId.get(id)), ks.reduce((t, k) => t + width.get(k), 0)));
        }
        const left = new Map([[c.root, 0]]);
        c.local = new Map();
        for (const id of c.order) {
          const w = width.get(id), l = left.get(id), ks = c.kids.get(id);
          c.local.set(id, [l + w / 2, c.depth.get(id) * GY]);
          let cur = l + (w - ks.reduce((t, k) => t + width.get(k), 0)) / 2;
          for (const k of ks) { left.set(k, cur); cur += width.get(k); }
        }
        c.w = width.get(c.root); c.h = (c.maxL + 1) * GY; c.GY = GY;
      } else {
        // 가지마다 각도 구간을 잎 수에 비례해 나눠 갖는다: 같은 가지의 개체는 같은 방향으로 모인다
        const step = Math.max(80, Math.min(130, 800 / Math.max(1, c.maxL)));
        const ring = [0];
        for (let L = 1; L <= c.maxL; L++) {
          const ns = c.order.filter((id) => c.depth.get(id) === L).map((id) => lw(byId.get(id)));
          ring[L] = Math.max(ring[L - 1] + step, (ns.reduce((a, b) => a + b, 0)) / (2 * Math.PI));   // 그 층 이름들이 한 고리에 다 들어갈 만큼
        }
        const range = new Map([[c.root, [-Math.PI / 2, (3 * Math.PI) / 2]]]);
        c.local = new Map();
        for (const id of c.order) {
          const [a0, a1] = range.get(id);
          const a = (a0 + a1) / 2, r = ring[c.depth.get(id)];
          c.local.set(id, [Math.cos(a) * r, Math.sin(a) * r]);
          const ks = c.kids.get(id), total = ks.reduce((t, k) => t + c.leaves.get(k), 0);
          let cur = a0;
          for (const k of ks) { const span = ((a1 - a0) * c.leaves.get(k)) / total; range.set(k, [cur, cur + span]); cur += span; }
        }
        c.ring = ring; c.R = ring[c.maxL] + 60; c.w = c.h = 2 * c.R;
      }
    };
    blocks.forEach(measure);

    // 덩어리들을 선반처럼 쌓는다. 한 줄의 폭은 화면 비율에 맞춰서, 좁은 창에서도 옆으로만 길어지지 않게 한다.
    const GAP = 80;
    const aspect = W && H ? W / H : 1.5;
    const maxW = Math.max(...blocks.map((c) => c.w));
    const limit = Math.max(maxW, Math.sqrt(blocks.reduce((t, c) => t + (c.w + GAP) * (c.h + GAP), 0) * aspect * 1.1));
    let x = 0, y = 0, shelfH = 0;
    for (const c of blocks) {
      if (x > 0 && x + c.w > limit) { x = 0; y += shelfH + GAP; shelfH = 0; }
      c.ox = x; c.oy = y;
      x += c.w + GAP; shelfH = Math.max(shelfH, c.h);
    }
    const bottom = y + shelfH;

    blocks.forEach((c, idx) => {
      const main = idx === 0;
      const cx = c.ox + (view.layout === "radial" ? c.R : 0), cy = c.oy + (view.layout === "radial" ? c.R : 0);
      for (const [id, [lx, ly]] of c.local) {
        const n = byId.get(id);
        if (view.layout === "tree") { n.tx = c.ox + lx; n.ty = c.oy + ly; } else { n.tx = cx + lx; n.ty = cy + ly; }
      }
      const rootN = byId.get(c.root);
      if (view.layout === "tree") {
        if (main) for (let L = 0; L <= c.maxL; L++) guides.push({ level: L, y: c.oy + L * c.GY, x0: c.ox, x1: c.ox + c.w, main });   // 단계선은 기준 덩어리에만
        if (!main) heads.push({ x: rootN.tx, y: rootN.ty - 30, text: `묶음 ${idx + 1}` });
      } else {
        if (main) for (let L = 1; L <= c.maxL; L++) guides.push({ level: L, r: c.ring[L], cx, cy, main });
        if (!main) heads.push({ x: cx, y: cy - c.R - 6, text: `묶음 ${idx + 1}` });
      }
    });

    // 연결이 하나도 없는 개체는 맨 아래에 이름 폭대로 여러 줄로 모은다
    if (singles.length) {
      singles.sort((a, b) => b.score - a.score);
      const y0 = bottom + GAP + 40;
      let cur = 0, row = 0;
      const rows = [[]];
      for (const n of singles) { const w = lw(n); if (cur + w > limit && rows[row].length) { row++; rows[row] = []; cur = 0; } rows[row].push(n); cur += w; }
      rows.forEach((r, ri) => { const total = r.reduce((t, n) => t + lw(n), 0); let px = (limit - total) / 2; for (const n of r) { n.tx = px + lw(n) / 2; n.ty = y0 + ri * 62; px += lw(n); } });
      heads.push({ x: limit / 2, y: y0 - 34, text: "연결 없는 개체" });
    }
    for (const n of vn) if (n.tx === undefined) { n.tx = n.x; n.ty = n.y; }
    treeAnim = true;
    frames = 0;
  }

  // 배치·깊이·기준 개체가 바뀔 때 보기를 다시 만든다
  function refreshView() {
    computeVisible();
    if (view.layout === "free") { guides = null; treeAnim = false; alpha = Math.max(alpha, 0.6); if (!userMoved) fit(); }
    else if (view.layout === "tree" || view.layout === "radial") layoutTree();
    applyLayoutMode();
    renderEmpty(); kick(); draw();
  }


  // ---------------------------------------------------------------- 분류 목록(개요)
  // 분류 관계('포함': 상위 → 하위)만으로 들여쓴 목록을 만든다. 지도는 그물이라 분류가 안 보이지만, 목록은 "묶음 아래 무엇이 있는가"를 한눈에 보여 준다.
  function applyLayoutMode() {
    const outline = view.layout === "outline";
    $r("#gpOutline").hidden = !outline;
    $r("#gpStage").hidden = outline;
    if (outline) renderOutline(); else draw();
  }

  function outlineTree() {
    const kids = new Map(nodes.map((n) => [n.id, []])), hasParent = new Set();
    for (const e of edges) if (e.category && kids.has(e.source) && kids.has(e.target) && e.source !== e.target) { kids.get(e.source).push(e.target); hasParent.add(e.target); }
    const size = new Map();
    const count = (id, path = new Set()) => {   // 하위 항목 수(순환이 있어도 멈춘다)
      if (size.has(id)) return size.get(id);
      if (path.has(id)) return 0;
      path.add(id);
      const n = kids.get(id).reduce((t, k) => t + 1 + count(k, path), 0);
      path.delete(id); size.set(id, n);
      return n;
    };
    for (const n of nodes) count(n.id);
    const order = (ids) => [...ids].sort((a, b) => (size.get(b) - size.get(a)) || byId.get(a).name.localeCompare(byId.get(b).name, "ko"));
    for (const [id, ks] of kids) kids.set(id, order(ks));
    const roots = order(nodes.filter((n) => kids.get(n.id).length && !hasParent.has(n.id)).map((n) => n.id));
    // 순환만으로 이뤄진 분류(부모가 있는데 뿌리에서 닿지 않는 것)도 빠지지 않게 마지막에 더한다
    const reach = new Set();
    const walk = (id) => { if (reach.has(id)) return; reach.add(id); kids.get(id).forEach(walk); };
    roots.forEach(walk);
    const stray = order(nodes.filter((n) => kids.get(n.id).length && !reach.has(n.id)).map((n) => n.id));
    stray.forEach((id) => { roots.push(id); walk(id); });
    const loose = nodes.filter((n) => !reach.has(n.id)).sort((a, b) => b.score - a.score);   // 분류에 속하지 않은 개체
    return { kids, size, roots, loose };
  }

  function renderOutline() {
    const box = $r("#gpOutline");
    if (!nodes.length) { box.innerHTML = `<div class="muted small outline-empty">개체가 없습니다.</div>`; return; }
    const { kids, size, roots, loose } = outlineTree();
    const q = "";
    const hit = () => false;
    const maxDepth = view.depth || 99;
    // 검색어가 있으면 일치하는 항목까지 펼치고, 나머지 가지는 접어 둔다
    const has = new Map();
    const matchIn = (id, path = new Set()) => {
      if (has.has(id)) return has.get(id);
      if (path.has(id)) return false;
      path.add(id);
      const r = hit(byId.get(id)) || kids.get(id).some((k) => matchIn(k, path));
      path.delete(id); has.set(id, r);
      return r;
    };
    const item = (id, depth, path) => {
      const n = byId.get(id), ks = kids.get(id).filter((k) => !path.has(k));
      const label = `<button type="button" class="ol-name${id === selected ? " sel" : ""}${hit(n) ? " hit" : ""}" data-ent="${id}"><i style="background:${colorOf(n)}"></i>${esc(n.name)}</button>`;
      const desc = n.desc ? `<span class="ol-desc">${esc(n.desc)}</span>` : "";
      if (!ks.length) return `<li class="ol-leaf">${label}<span class="muted small">${esc(n.type)}</span>${desc}</li>`;
      const open = q ? matchIn(id) : depth < Math.min(maxDepth, 2) || id === selected;
      const body = depth >= maxDepth
        ? `<div class="muted small ol-more">하위 ${size.get(id)}개 (깊이 제한으로 접음)</div>`
        : `<ul>${ks.map((k) => item(k, depth + 1, new Set([...path, id]))).join("")}</ul>`;
      return `<li><details${open ? " open" : ""}><summary>${label}<span class="ol-count">${size.get(id)}</span>${desc}</summary>${body}</details></li>`;
    };
    let html = roots.length
      ? `<div class="ol-tools"><button type="button" data-ol="open">모두 펼치기</button><button type="button" data-ol="close">모두 접기</button><span class="muted small">분류 ${roots.length}개 · 분류된 개체 ${nodes.length - loose.length}개</span></div>`
      + `<ul class="outline">${roots.map((id) => item(id, 0, new Set())).join("")}</ul>`
      : `<div class="muted small outline-empty">분류 관계가 없습니다.</div>`;
    if (loose.length) {
      html += `<details class="ol-loose"${roots.length ? "" : " open"}><summary><b>분류 없는 개체</b> <span class="ol-count">${loose.length}</span></summary><div class="ol-flat">${loose.map((n) => `<button type="button" class="ol-name${n.id === selected ? " sel" : ""}${hit(n) ? " hit" : ""}" data-ent="${n.id}"><i style="background:${colorOf(n)}"></i>${esc(n.name)}</button>`).join("")}</div></details>`;
    }
    box.innerHTML = html;
  }

  $r("#gpOutline").addEventListener("click", (e) => {
    const tool = e.target.closest("[data-ol]")?.dataset.ol;
    if (tool) { $r("#gpOutline").querySelectorAll("details").forEach((d) => { d.open = tool === "open"; }); return; }
    const b = e.target.closest("[data-ent]");
    if (b) { e.preventDefault(); select(Number(b.dataset.ent)); }   // summary 안의 버튼: 접고 펴지 않고 선택만
  });

  // ---------------------------------------------------------------- 힘 기반 배치
  function tick(a) {
    const n = vn.length;
    const spread = Math.min(2.6, Math.max(1, Math.sqrt(n / 30)));   // 개체가 많을수록 더 넓게
    const reach2 = (400 * spread) ** 2;
    for (let i = 0; i < n; i++) {
      const p = vn[i];
      for (let j = i + 1; j < n; j++) {
        const q = vn[j];
        let dx = q.x - p.x, dy = q.y - p.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 0.01) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 0.5; }
        if (d2 > reach2) continue;
        const f = (520 * spread * a) / d2;
        p.vx -= dx * f; p.vy -= dy * f; q.vx += dx * f; q.vy += dy * f;
      }
    }
    for (const e of ve) {
      const dx = e.b.x - e.a.x, dy = e.b.y - e.a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const rest = (70 + (e.a.r + e.b.r) * 0.6) * Math.sqrt(spread);
      const k = ((d - rest) / d) * 0.12 * a * Math.min(2, 0.6 + e.weight * 0.4);
      e.a.vx += dx * k; e.a.vy += dy * k; e.b.vx -= dx * k; e.b.vy -= dy * k;
    }
    for (const p of vn) {
      const g = (p.degree === 0 ? 0.1 : 0.02) * a;   // 연결이 없는 개체는 멀리 떠내려가지 않게 가운데로 더 세게 당긴다
      p.vx -= p.x * g; p.vy -= p.y * g;
      if (p.fixed) { p.vx = p.vy = 0; continue; }
      p.vx *= 0.6; p.vy *= 0.6;
      p.x += p.vx; p.y += p.vy;
    }
  }

  function frame() {
    raf = 0;
    if (dead) return;
    if (view.layout === "free") {
      if (alpha > 0.012) { tick(alpha); alpha *= 0.985; if (!userMoved && ++frames % 6 === 0) fit(); }
    } else if (treeAnim) {                       // 계층·방사형: 목표 자리로 부드럽게 옮긴다
      let moving = false;
      for (const n of vn) {
        const dx = n.tx - n.x, dy = n.ty - n.y;
        if (Math.abs(dx) > 0.4 || Math.abs(dy) > 0.4) { n.x += dx * 0.2; n.y += dy * 0.2; moving = true; } else { n.x = n.tx; n.y = n.ty; }
      }
      if (!userMoved && (++frames % 4 === 0 || !moving)) fit();
      if (!moving) treeAnim = false;
    }
    draw();
    if ((view.layout === "free" && alpha > 0.012) || treeAnim || drag) raf = requestAnimationFrame(frame);
  }
  function kick() { if (!raf && !dead) raf = requestAnimationFrame(frame); }

  // ---------------------------------------------------------------- 그리기
  const css = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  const colorOf = (n) => (opts.color === "group" ? GROUP_COLORS[n.community % GROUP_COLORS.length] : TYPE_COLORS[n.type] || TYPE_COLORS["기타"]);
  const sx = (x) => x * tf.k + tf.x, sy = (y) => y * tf.k + tf.y;
  const rad = (n) => n.r * Math.min(1.6, Math.max(0.55, Math.sqrt(tf.k)));

  function resize() {
    const rect = stage.getBoundingClientRect();
    W = Math.max(0, Math.floor(rect.width)); H = Math.max(0, Math.floor(rect.height));
    dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, W * dpr); canvas.height = Math.max(1, H * dpr);
    canvas.style.width = `${W}px`; canvas.style.height = `${H}px`;
    // 창 모양이 크게 바뀌면(전체 화면·창 크기 조절) 계층·방사형은 덩어리 쌓기를 화면 비율에 맞춰 다시 한다
    if ((view.layout === "tree" || view.layout === "radial") && vn.length && W > 0 && Math.abs(W / Math.max(1, H) - lastAspect) > 0.2) { lastAspect = W / Math.max(1, H); layoutTree(); userMoved = false; }
    if (W > 0 && needFit && nodes.length) fit();
    kick(); draw();
  }

  // 자유 배치에서 전체를 볼 때만, 선택·가리킨 개체와 그 이웃을 뺀 나머지를 흐리게 한다
  const focusDims = () => view.layout === "free" && view.depth === 0;
  function isDim(n) {
    if (matches) return !matches.has(n.id);
    const focus = selected ?? hover;
    return focusDims() && focus != null && n.id !== focus && !adj.get(focus)?.has(n.id);
  }

  function draw() {
    if (!W || !H) return;
    const text = css("--text", "#22201c"), bg = css("--surface", "#fff"), line = css("--line", "#ccc"), muted = css("--muted", "#777");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const focus = selected ?? hover;
    // 글자는 모두 한 목록(placed)에서 겹침을 검사한다: 머리글 → 단계 이름 → 개체 이름 순으로 자리를 잡는다
    const placed = []; lastBoxes = placed;
    const putText = (t, x, y, align, force) => {
      const w = ctx.measureText(t).width;
      const x0 = align === "right" ? x - w : align === "left" ? x : x - w / 2;
      const box = [x0 - 2, y - 1, x0 + w + 2, y + 15];
      if (!force && (box[2] < 0 || box[0] > W || box[3] < 0 || box[1] > H || placed.some((r) => box[0] < r[2] && box[2] > r[0] && box[1] < r[3] && box[3] > r[1]))) return false;
      placed.push(box);
      ctx.textAlign = align; ctx.lineWidth = 3; ctx.strokeStyle = bg; ctx.strokeText(t, x, y);
      ctx.fillStyle = text; ctx.fillText(t, x, y);
      return true;
    };
    ctx.textBaseline = "top";
    if (view.layout === "tree" || view.layout === "radial") {
      ctx.strokeStyle = line; ctx.lineWidth = 1; ctx.globalAlpha = 0.7; ctx.setLineDash([4, 5]);
      for (const gd of guides || []) {
        ctx.beginPath();
        if (view.layout === "tree") { ctx.moveTo(sx(gd.x0) - 8, sy(gd.y)); ctx.lineTo(sx(gd.x1) + 8, sy(gd.y)); ctx.stroke(); }
        else if (gd.r > 0) { ctx.arc(sx(gd.cx), sy(gd.cy), gd.r * tf.k, 0, Math.PI * 2); ctx.stroke(); }
      }
      ctx.setLineDash([]); ctx.globalAlpha = 1;
      ctx.font = 'bold 12px system-ui, "Malgun Gothic", sans-serif';
      for (const hd of heads) putText(hd.text, sx(hd.x), sy(hd.y), "center", false);
      ctx.font = '12px system-ui, "Malgun Gothic", sans-serif';
      for (const gd of guides || []) {
        const name = gd.level === 0 ? "기준" : `${gd.level}단계`;
        if (view.layout === "tree") putText(name, sx(gd.x0) - 10, sy(gd.y) - 7, "right", false);
        else if (gd.r > 0) putText(name, sx(gd.cx), sy(gd.cy) - gd.r * tf.k - 16, "center", false);
      }
    }
    // 간선
    for (const e of ve) {
      const near = focus != null && (e.source === focus || e.target === focus);
      const dim = (focusDims() && focus != null && !near) || (matches && !(matches.has(e.source) && matches.has(e.target)));
      const x1 = sx(e.a.x), y1 = sy(e.a.y), x2 = sx(e.b.x), y2 = sy(e.b.y);
      ctx.globalAlpha = dim ? 0.12 : near ? 0.95 : 0.55;
      ctx.strokeStyle = near ? css("--accent", "#3563e9") : line;
      ctx.lineWidth = Math.min(4, 1 + Math.log2(1 + e.weight) * 0.6) * (near ? 1.4 : 1);
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
      // 방향 화살표 (도착 노드 가장자리)
      const dx = x2 - x1, dy = y2 - y1, d = Math.hypot(dx, dy) || 1, ux = dx / d, uy = dy / d, rb = rad(e.b);
      if (d > rb + 14 && (!dim)) {
        const tx = x2 - ux * (rb + 1), ty = y2 - uy * (rb + 1);
        ctx.fillStyle = ctx.strokeStyle;
        ctx.beginPath(); ctx.moveTo(tx, ty); ctx.lineTo(tx - ux * 8 - uy * 4, ty - uy * 8 + ux * 4); ctx.lineTo(tx - ux * 8 + uy * 4, ty - uy * 8 - ux * 4); ctx.closePath(); ctx.fill();
      }
    }
    // 관계 이름
    ctx.globalAlpha = 1; ctx.font = '11px system-ui, "Malgun Gothic", sans-serif'; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    for (const e of ve) {
      const near = focus != null && (e.source === focus || e.target === focus);
      if (!(near || (opts.edgeLabels && tf.k > 0.9 && !matches))) continue;
      const mx = (sx(e.a.x) + sx(e.b.x)) / 2, my = (sy(e.a.y) + sy(e.b.y)) / 2;
      const label = e.labels.slice(0, 2).join(" / ");
      ctx.lineWidth = 3; ctx.strokeStyle = bg; ctx.strokeText(label, mx, my);
      ctx.fillStyle = near ? text : muted; ctx.fillText(label, mx, my);
    }
    // 노드
    for (const n of vn) {
      const dim = isDim(n);
      ctx.globalAlpha = dim ? 0.18 : 1;
      const x = sx(n.x), y = sy(n.y), r = rad(n);
      ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fillStyle = colorOf(n); ctx.fill();
      ctx.lineWidth = n.id === selected ? 3 : 1.5;
      ctx.strokeStyle = n.id === selected ? text : bg; ctx.stroke();
    }
    // 노드 이름: 선택한 개체는 항상, 나머지는 중요한 것(찾은 것·선택 주변 → 중요도순)부터 놓고 겹치면 건너뛴다. 확대하면 더 보인다.
    ctx.globalAlpha = 1; ctx.font = LABEL_FONT; ctx.textBaseline = "top";
    const rank = (n) => (n.id === selected ? 3 : matches?.has(n.id) ? 2 : focusDims() && focus != null && adj.get(focus)?.has(n.id) ? 1 : 0);
    const order = [...vn].sort((a, b) => rank(b) - rank(a) || b.score - a.score);
    for (const n of order) {
      if (isDim(n)) continue;
      putText(n.name, sx(n.x), sy(n.y) + rad(n) + 2, "center", rank(n) === 3);
    }
    ctx.textAlign = "center";
    ctx.globalAlpha = 1;
  }

  // ---------------------------------------------------------------- 보기 조작
  function fit() {
    if (!vn.length || !W || !H) return;
    needFit = false;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of vn) { x0 = Math.min(x0, n.x - n.r); y0 = Math.min(y0, n.y - n.r); x1 = Math.max(x1, n.x + n.r); y1 = Math.max(y1, n.y + n.r); }
    const padL = view.layout === "tree" ? 60 : 0;   // 계층은 왼쪽에 단계 이름이 들어갈 자리를 남긴다
    const k = Math.min((W - 90 - padL) / Math.max(1, x1 - x0), (H - 90) / Math.max(1, y1 - y0), 2);
    tf = { k, x: W / 2 + padL / 2 - ((x0 + x1) / 2) * k, y: H / 2 - ((y0 + y1) / 2) * k };
    draw();
  }
  function zoomAt(px, py, factor) {
    userMoved = true;
    const k = Math.min(6, Math.max(0.15, tf.k * factor));
    const f = k / tf.k;
    tf = { k, x: px - (px - tf.x) * f, y: py - (py - tf.y) * f };
    kick(); draw();
  }
  const local = (e) => { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  function nodeAt(px, py) {
    for (let i = vn.length - 1; i >= 0; i--) {
      const n = vn[i];
      if (Math.hypot(sx(n.x) - px, sy(n.y) - py) <= rad(n) + 3) return n;
    }
    return null;
  }

  canvas.style.touchAction = "none";
  canvas.addEventListener("pointerdown", (e) => {
    canvas.setPointerCapture(e.pointerId);
    const [px, py] = local(e);
    const n = nodeAt(px, py);
    drag = { node: n, px, py, moved: false, tx: tf.x, ty: tf.y };
  });
  canvas.addEventListener("pointermove", (e) => {
    const [px, py] = local(e);
    if (drag) {
      if (Math.hypot(px - drag.px, py - drag.py) > 4) drag.moved = true;
      if (drag.moved) {
        if (drag.node) {
          userMoved = true;
          drag.node.fixed = true; drag.node.x = (px - tf.x) / tf.k; drag.node.y = (py - tf.y) / tf.k; alpha = Math.max(alpha, 0.25);
          drag.node.tx = drag.node.x; drag.node.ty = drag.node.y;
        }
        else { tf.x = drag.tx + px - drag.px; tf.y = drag.ty + py - drag.py; userMoved = true; }
        kick(); draw();
      }
      return;
    }
    const n = nodeAt(px, py);
    if ((n?.id ?? null) !== hover) { hover = n?.id ?? null; canvas.style.cursor = n ? "pointer" : "grab"; draw(); }
  });
  const endDrag = (e) => {
    if (!drag) return;
    const d = drag; drag = null;
    if (d.node) d.node.fixed = false;
    if (!d.moved) select(d.node ? d.node.id : null);
  };
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", () => { if (drag?.node) drag.node.fixed = false; drag = null; });
  canvas.addEventListener("pointerleave", () => { if (!drag && hover !== null) { hover = null; draw(); } });
  canvas.addEventListener("wheel", (e) => { e.preventDefault(); const [px, py] = local(e); zoomAt(px, py, Math.pow(1.0018, -e.deltaY)); }, { passive: false });
  $r(".graph-zoom").addEventListener("click", (e) => {
    const z = e.target.closest("[data-z]")?.dataset.z;
    if (z === "in") zoomAt(W / 2, H / 2, 1.3);
    if (z === "out") zoomAt(W / 2, H / 2, 1 / 1.3);
    if (z === "fit") { userMoved = false; fit(); }
    if (z === "full") {
      if (document.fullscreenElement) document.exitFullscreen();
      else $r("#gpFull").requestFullscreen?.().catch(() => { /* 브라우저가 거절하면 그대로 둔다 */ });
    }
  });

  // ---------------------------------------------------------------- 선택·옆 패널
  function select(id) {
    selected = id;
    if (view.layout === "outline") renderOutline();
    else if (view.depth > 0 || view.layout !== "free") refreshView(); else { draw(); }
    renderSide();
  }

  function centerOn(n) {
    userMoved = true;
    tf = { ...tf, x: W / 2 - n.x * tf.k, y: H / 2 - n.y * tf.k };
    draw();
  }

  async function renderSide() {
    const side = $r("#gpSide");
    if (selected == null) {
      const top = [...nodes].sort((a, b) => b.score - a.score).slice(0, 30);
      side.innerHTML = nodes.length
        ? `<div class="side-title">핵심 개체 <span class="muted small">중요도순</span></div>
           <div class="muted small">지도에서 개체를 누르면 관계와 출처가 여기에 나옵니다. 끌어서 옮기고, 휠로 확대할 수 있습니다.</div>
           <div class="ent-list">${top.map((n) => `<button type="button" class="ent" data-ent="${n.id}"><i style="background:${colorOf(n)}"></i>${esc(n.name)}<span class="muted small">${n.mentions}</span></button>`).join("")}</div>
           ${data.truncated ? `<div class="muted small">개체가 ${data.total_entities}개라서 중요한 ${nodes.length}개만 그렸습니다. 위에서 표시 개체 수를 늘릴 수 있습니다.</div>` : ""}`
        : `<div class="muted small">개체가 없습니다.</div>`;
      return;
    }
    const token = ++detailToken;
    const n = byId.get(selected);
    side.innerHTML = `<div class="side-title">${esc(n?.name ?? "")}</div><div class="muted small">불러오는 중…</div>`;
    let d;
    try { d = await api(`/api/agents/${agentId}/graph/entities/${selected}`); }
    catch (err) { if (token === detailToken) side.innerHTML = `<div class="form-error">${esc(err.message)}</div>`; return; }
    if (token !== detailToken || dead) return;
    const rels = d.relations.map((r) => `<li><button type="button" class="link" data-ent="${r.other_id}">${esc(r.other)}</button>
      <span class="muted small">${r.direction === "out" ? "→ 이 개체가" : "← 상대가"} · ${esc(r.label)}${r.count > 1 ? ` ×${r.count}` : ""}</span></li>`).join("");
    const srcs = d.chunks.map((c) => `<div class="src"><div class="src-head"><b>${esc(c.filename)}</b>${c.page ? ` · ${c.page}쪽` : ""}</div><div class="src-text">${esc(c.snippet)}</div></div>`).join("");
    side.innerHTML = `
      <div class="side-title">${esc(d.name)} <span class="pill" style="border-color:${TYPE_COLORS[d.type] || TYPE_COLORS["기타"]}">${esc(d.type)}</span></div>
      ${d.desc ? `<div class="ent-desc">${esc(d.desc)}</div>` : ""}
      <div class="muted small">${d.mentions}개 조각에 나옵니다</div>
      <button type="button" class="link" data-ent="">← 전체 보기</button>
      <div class="side-h">관계 ${d.relations.length}</div>
      ${rels ? `<ul class="rel-list">${rels}</ul>` : `<div class="muted small">연결된 개체가 없습니다.</div>`}
      <div class="side-h">나온 곳</div>${srcs || `<div class="muted small">-</div>`}`;
  }

  $r("#gpSide").addEventListener("click", (e) => {
    const b = e.target.closest("[data-ent]");
    if (!b) return;
    const id = b.dataset.ent === "" ? null : Number(b.dataset.ent);
    select(id);
    const n = id != null ? byId.get(id) : null;
    if (n && view.layout === "free" && view.depth === 0) centerOn(n);
  });

  function renderLegend() {
    const el = $r("#gpLegend");
    if (!nodes.length) { el.innerHTML = ""; return; }
    if (opts.color === "group") {
      const gs = [...new Set(nodes.map((n) => n.community))].sort((a, b) => a - b).slice(0, 11);
      el.innerHTML = gs.map((g) => `<span><i style="background:${GROUP_COLORS[g % GROUP_COLORS.length]}"></i>묶음 ${g + 1}</span>`).join("") + `<span class="muted small">같은 색은 서로 많이 연결된 개체들입니다</span>`;
    } else {
      const used = new Set(nodes.map((n) => n.type));
      el.innerHTML = Object.keys(TYPE_COLORS).filter((t) => used.has(t)).map((t) => `<span><i style="background:${TYPE_COLORS[t]}"></i>${t}</span>`).join("")
        + `<span class="muted small">원이 클수록 중요한 개체입니다</span>`;
    }
  }

  // ---------------------------------------------------------------- 불러오기 (만드는 중에는 계속 확인)
  async function load() {
    try { applyData(await api(`/api/agents/${agentId}/graph?limit=${opts.limit}`)); }
    catch (err) { $r("#gpMsg").innerHTML = `<div class="form-error">${esc(err.message)}</div>`; }
    schedule();
  }
  async function refresh() {
    if (dead) return;
    let st;
    try { st = await api(`/api/agents/${agentId}/graph/status`); } catch { return schedule(); }
    if (dead) return;
    const changed = !status || st.done_chunks !== status.done_chunks || st.job.state !== status.job.state || st.entities !== status.entities;
    status = st;
    if (changed || lastDone < 0) { lastDone = st.done_chunks; await load(); return; }
    renderStatus();
    schedule();
  }
  function schedule() {
    clearTimeout(timer);
    if (!dead && status?.job.state === "running") timer = setTimeout(refresh, 1500);
  }

  root.addEventListener("graph-shown", () => { needFit = needFit || !nodes.length; resize(); load(); });
  const onFull = () => { userMoved = false; setTimeout(() => { resize(); fit(); }, 60); };
  document.addEventListener("fullscreenchange", onFull);
  if (!document.fullscreenEnabled) $r("#gpFullBtn").hidden = true;
  const ro = new ResizeObserver(resize);
  ro.observe(stage);
  matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => draw());
  const watcher = setInterval(() => { if (!root.isConnected) destroy(); }, 2000);
  function destroy() { dead = true; clearTimeout(timer); clearInterval(watcher); ro.disconnect(); document.removeEventListener("fullscreenchange", onFull); if (raf) cancelAnimationFrame(raf); }

  // 시험에서 화면 좌표를 얻기 위한 읽기 전용 손잡이
  canvas.__gv = {
    nodes: () => vn.map((n) => ({ id: n.id, name: n.name, type: n.type, x: sx(n.x), y: sy(n.y), r: rad(n) })),
    selected: () => selected, fullscreen: () => document.fullscreenElement?.id === "gpFull", transform: () => ({ ...tf }), size: () => [W, H], settled: () => (view.layout === "free" ? alpha <= 0.012 : !treeAnim), layout: () => view.layout, depth: () => view.depth, hidden: () => nodes.length - vn.length, dimmed: () => vn.filter((n) => isDim(n)).length, root: () => rootId(), labelBoxes: () => lastBoxes.map((b) => [...b]),
  };

  resize();
  renderStatus();
  load();
  return { destroy };
}
