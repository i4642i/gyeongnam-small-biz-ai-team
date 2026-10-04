// 캐릭터 만들기의 '모델' 탭과 '업무·출력' 탭. $, api, esc 는 app.js 에서 온다.
//   createModelPicker(root, onChange)  → { getValue(), validate() }
// 두 편집기의 입력칸에는 name 속성을 달지 않는다(폼의 FormData 에 섞이지 않게). 값은 getValue() 로만 꺼낸다.

const PROVIDER_SHORT = { anthropic: "Claude", openai: "OpenAI", google: "Gemini" };
const modelTag = (m) => (m && m.model ? `${PROVIDER_SHORT[m.provider] ?? m.provider} · ${m.model}` : "");

// 입력칸 안에서 Enter 를 눌러도 만들기 폼이 제출되지 않게 한다 (여러 줄 입력칸은 제외)
function blockEnterSubmit(root) {
  root.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.tagName === "INPUT" && e.target.type !== "submit") e.preventDefault();
  });
}

// ---------------------------------------------------------------- 모델 탭

// options.initial: {provider, model} 로 시작(수정 화면). options.originalProvider: 이 공급사에서 바뀌면 전송 대상이 바뀐다는 안내를 띄운다.
function createModelPicker(root, onChange = () => {}, options = {}) {
  let providers = [];
  let loadError = "";
  let custom = false;                       // 목록에서 고르지 않고 직접 입력 중인가
  const state = { provider: options.initial?.provider ?? "anthropic", model: options.initial?.model ?? "" };
  const mock = $("#mode")?.textContent === "mock";

  root.innerHTML = `
    <p class="muted small">이 캐릭터가 대화와 업무에 쓸 LLM을 고릅니다. 캐릭터마다 다르게 정할 수 있습니다.
      API 키는 화면에서 입력하지 않고, 서버 폴더의 <code>.env</code> 파일에 넣어 둔 것을 씁니다.</p>
    ${mock ? `<div class="notice">지금은 서버에 API 키가 하나도 없어서 <b>가짜 답변(mock)</b>으로 동작합니다. 모델 설정은 그대로 저장됩니다.</div>` : ""}
    <div id="mpSwitch"></div>
    <div id="mpProviders" class="provider-list"><span class="muted">공급사 정보를 불러오는 중…</span></div>
    <div id="mpModel" class="field"></div>`;
  blockEnterSubmit(root);

  const current = () => providers.find((p) => p.id === state.provider);
  // 목록이 보이는 공급사는 선택 상자가 첫 모델을 보여 주므로, 내부 값도 같은 모델이어야 한다
  // (그렇지 않으면 화면에는 골라진 것처럼 보이는데 저장 때 '모델 이름을 입력해 주세요'가 뜬다)
  const defaultModel = (p) => {
    if (!p) return "";
    const list = p.models || [];
    // 공급사가 정한 기본 모델이 목록에 있으면 그것, 없으면(공급사가 목록에서 뺀 경우) 목록의 첫 모델
    return list.includes(p.default_model) ? p.default_model : (list[0] || p.default_model || "");
  };

  function drawSwitchNotice() {
    const from = options.originalProvider;
    const changed = from && state.provider !== from && providers.length;
    const label = (id) => providers.find((p) => p.id === id)?.label ?? id;
    $("#mpSwitch", root).innerHTML = changed
      ? `<div class="notice warn">공급사를 <b>${esc(label(from))}</b>에서 <b>${esc(label(state.provider))}</b>(으)로 바꿉니다. 이후 이 캐릭터와의 대화 내용은 새 공급사로 전송됩니다(전문자료를 붙이면 문서 조각도 함께). 지금까지의 대화 기록과 설정은 그대로 이어집니다.</div>`
      : "";
  }

  function draw() {
    drawSwitchNotice();
    const box = $("#mpProviders", root);
    if (!providers.length) {
      box.innerHTML = `<span class="muted">${esc(loadError || "공급사 정보를 불러오는 중…")}</span>`;
    } else {
      box.innerHTML = providers.map((p) => `
        <label class="provider${p.id === state.provider ? " on" : ""}">
          <input type="radio" name="provider" value="${p.id}" ${p.id === state.provider ? "checked" : ""} />
          <span class="pname">${esc(p.label)}</span>
          <span class="pill ${p.configured ? "ok" : "warn"}">${p.configured ? "키 설정됨" : "키 없음"}</span>
          ${p.configured ? "" : `<small class="muted">.env 의 ${esc(p.key_hint)}</small>`}
        </label>`).join("");
    }

    const p = current();
    const models = p?.models ?? [];
    const inList = models.includes(state.model);
    const showSelect = models.length > 0;
    const showText = !showSelect || custom || (state.model && !inList);
    const hint = !p ? ""
      : p.models_source === "live"
        ? "공급사에서 불러온 최신 목록입니다. 목록에 없으면 '직접 입력'을 고르세요."
        : showSelect
          ? `내장된 목록입니다(공식 문서 기준 ${p.models_checked} 확인). 새로 나온 모델은 없을 수 있으니 목록에 없으면 '직접 입력'을 고르세요.`
            + (p.configured ? " (지금은 공급사의 최신 목록을 불러오지 못해 내장 목록을 보여 줍니다.)" : " 키를 넣으면 공급사의 최신 목록을 불러옵니다.")
          : "모델 이름을 직접 입력하세요.";
    $("#mpModel", root).innerHTML = `
      <label><span>모델 <span class="req">*</span></span>
        ${showSelect ? `<select id="mpSelect">
            ${models.map((m) => `<option value="${esc(m)}"${m === state.model && !custom ? " selected" : ""}>${esc(m)}</option>`).join("")}
            <option value="__custom"${showText ? " selected" : ""}>직접 입력…</option>
          </select>` : ""}
        ${showText ? `<input type="text" id="mpText" maxlength="80" placeholder="모델 이름" value="${esc(state.model)}" />` : ""}
      </label>
      <div class="muted small">${esc(hint)}</div>`;
  }

  root.addEventListener("change", (e) => {
    if (e.target.name === "provider") {
      state.provider = e.target.value;
      state.model = defaultModel(current());
      custom = false;
      draw();
      onChange();
    } else if (e.target.id === "mpSelect") {
      custom = e.target.value === "__custom";
      state.model = custom ? "" : e.target.value;
      draw();
      if (custom) $("#mpText", root)?.focus();
      onChange();
    }
  });
  root.addEventListener("input", (e) => {
    if (e.target.id === "mpText") { state.model = e.target.value; onChange(); }
  });

  api("/api/llm/providers")
    .then((list) => {
      providers = list;
      if (!state.model) state.model = defaultModel(current());
      draw();
      onChange();
    })
    .catch((err) => { loadError = `공급사 정보를 불러오지 못했습니다: ${err.message}`; draw(); });
  draw();

  return {
    getValue: () => ({ provider: state.provider, model: state.model.trim() }),
    validate: () => (state.model.trim() ? null : `${PROVIDER_SHORT[state.provider]} 모델 이름을 입력해 주세요.`),
  };
}
