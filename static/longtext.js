// 긴 글 입력칸 도우미: 글자 수·대략의 토큰 수 표시, 마크다운/텍스트/JSON 파일로 채우기.
//   LongText.wire(textarea)         입력칸 아래에 표시줄을 붙인다(제한은 textarea 의 maxlength). 다시 그려도 여러 번 불러도 안전하다.
//   textarea._ltUpdate()            값을 코드로 바꾼 뒤 표시를 새로 고칠 때
// 토큰 수는 추정이다(영문·숫자 4자 ≈ 1토큰, 한글 등 1자 ≈ 1.2토큰으로 어림). 실제 값은 모델마다 ±30% 정도 다를 수 있다.

const LongText = (() => {
  const MAX_FILE_BYTES = 500 * 1024;
  const nf = (n) => n.toLocaleString("ko-KR");

  function estimateTokens(text) {
    let ascii = 0, other = 0;
    for (const ch of text) (ch.charCodeAt(0) < 128 ? ascii++ : other++);
    return Math.round(ascii / 4 + other * 1.2);
  }

  const summary = (text, limit) => `${nf(text.length)} / ${nf(limit)}자 · 약 ${nf(estimateTokens(text))} 토큰`;

  // 파일 → 글. 조용히 자르지 않는다: 제한을 넘거나 UTF-8 이 아니면 이유를 알려 주고 채우지 않는다.
  async function readFile(file, limit) {
    if (file.size > MAX_FILE_BYTES) throw new Error(`파일이 너무 큽니다(${Math.round(file.size / 1024)}KB, 최대 ${MAX_FILE_BYTES / 1024}KB).`);
    let text;
    try {
      text = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
    } catch {
      throw new Error("UTF-8 텍스트 파일이 아닙니다. 메모장에서 「다른 이름으로 저장 → 인코딩: UTF-8」로 저장한 뒤 다시 올려 주세요.");
    }
    text = text.replace(/^﻿/, "").replace(/\r\n/g, "\n");
    if (text.length > limit) throw new Error(`파일이 ${nf(text.length)}자라 이 칸의 제한(${nf(limit)}자)을 넘습니다. 잘라서 넣지 않았습니다 — 파일을 줄이거나 나눠 주세요.`);
    return text;
  }

  function wire(ta) {
    if (!ta || ta._ltBar) { ta?._ltUpdate?.(); return; }
    const limit = ta.maxLength > 0 ? ta.maxLength : 20000;
    const bar = document.createElement("div");
    bar.className = "longtext-bar";
    bar.innerHTML = `<span class="counter" aria-live="polite"></span><span class="lt-msg" role="alert"></span>
      <button type="button" class="link" data-fill title="마크다운(.md)·텍스트(.txt) 파일 내용으로 이 칸을 채웁니다">📄 파일에서 채우기</button>`;
    (ta.closest("label") || ta).after(bar);
    ta._ltBar = bar;

    const counter = bar.querySelector(".counter"), msg = bar.querySelector(".lt-msg");
    const update = () => {
      counter.textContent = summary(ta.value, limit);
      counter.classList.toggle("near", ta.value.length > limit * 0.9);
    };
    ta._ltUpdate = update;
    ta.addEventListener("input", () => { msg.textContent = ""; msg.classList.remove("ok"); update(); });

    bar.querySelector("[data-fill]").addEventListener("click", () => {
      const input = document.createElement("input");
      input.type = "file";
      input.accept = ".md,.markdown,.txt,.json,text/markdown,text/plain,application/json";
      input.addEventListener("change", async () => {
        const file = input.files[0];
        if (!file) return;
        msg.textContent = ""; msg.classList.remove("ok");
        try {
          const text = await readFile(file, limit);
          if (ta.value.trim() && !confirm(`이미 적힌 내용을 「${file.name}」의 내용으로 바꿀까요?`)) return;
          ta.value = text;
          ta.dispatchEvent(new Event("input", { bubbles: true }));
          msg.textContent = `✔ ${file.name} 내용으로 채웠습니다.`; msg.classList.add("ok");
        } catch (err) {
          msg.textContent = err.message;
        }
      });
      input.click();
    });
    update();
  }

  return { wire, estimateTokens, summary };
})();
