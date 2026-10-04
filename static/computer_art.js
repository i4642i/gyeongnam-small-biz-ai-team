// 회사 컴퓨터 그림(직원 복셀 아바타와 어울리게 블록 모양). 모니터 화면 색이 상태를 나타낸다.
//   computerSvg(state, size) → SVG 글. state: ready(초록) | busy(주황, 깜빡임) | failed(빨강) | empty(회색)
const COMPUTER_SCREEN = {
  ready: ["#2f9e6b", "#7ee2b3"], busy: ["#d9822b", "#ffd08a"], failed: ["#c9453a", "#ff9b91"], empty: ["#5b6470", "#9aa4b1"],
};
const COMPUTER_STATE_TEXT = { ready: "준비됨", busy: "받는 중·색인 중", failed: "실패 있음", empty: "비어 있음" };

function computerSvg(state = "empty", size = 96) {
  const [scr, glow] = COMPUTER_SCREEN[state] || COMPUTER_SCREEN.empty;
  const blink = state === "busy" ? `<animate attributeName="opacity" values="1;.35;1" dur="1.2s" repeatCount="indefinite" />` : "";
  // 앞면·옆면·윗면 세 가지 명암으로 블록을 만든다(빛은 왼쪽 위).
  const box = (x, y, w, h, d, front, side, top) =>
    `<polygon points="${x},${y} ${x + d},${y - d} ${x + w + d},${y - d} ${x + w},${y}" fill="${top}"/>` +
    `<polygon points="${x + w},${y} ${x + w + d},${y - d} ${x + w + d},${y + h - d} ${x + w},${y + h}" fill="${side}"/>` +
    `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${front}"/>`;
  return `<svg class="pc-art" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="${size}" height="${size}" role="img" aria-label="컴퓨터 — ${COMPUTER_STATE_TEXT[state] || ""}">
    <ellipse cx="52" cy="90" rx="42" ry="5" fill="rgba(0,0,0,.18)"/>
    ${box(64, 36, 22, 52, 7, "#cfd5dc", "#a9b1bb", "#e8ecf0")}
    <rect x="68" y="42" width="14" height="3" fill="#8a939e"/><rect x="68" y="48" width="14" height="3" fill="#8a939e"/>
    <rect x="70" y="76" width="5" height="5" fill="${glow}">${blink}</rect>
    ${box(30, 76, 10, 8, 4, "#9aa3ad", "#7c858f", "#b9c0c8")}
    ${box(12, 30, 46, 40, 7, "#d8dde3", "#b1b9c2", "#eef1f4")}
    <rect x="16" y="34" width="38" height="31" fill="#27303b"/>
    <rect x="18" y="36" width="34" height="27" fill="${scr}">${blink}</rect>
    <rect x="21" y="40" width="16" height="3" fill="${glow}" opacity=".9"/><rect x="21" y="46" width="24" height="3" fill="${glow}" opacity=".7"/>
    <rect x="21" y="52" width="12" height="3" fill="${glow}" opacity=".6"/>
    ${box(14, 86, 40, 4, 3, "#7a838d", "#5f6770", "#a3abb4")}
  </svg>`;
}
