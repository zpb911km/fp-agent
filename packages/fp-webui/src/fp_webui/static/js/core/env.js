/* ═══════════════════════════════════════════════════════════
   FP WebUI — 输入环境探测（触屏 / 实体键盘）
   ───────────────────────────────────────────────────────────
   为什么需要：Enter 的语义取决于**有没有 Shift 键**。
   桌面：Enter=发送、Shift+Enter=换行（快）；
   触屏：没有 Shift，Enter 若发送就永远写不出第二行 → Enter=换行、
        发送靠 ➤ 按钮（唯一可行解）。
   判据用媒体查询而非 UA 嗅探：UA 在 iPad/安卓 WebView 上不可靠。
   · pointer: coarse    → 主动输入是触屏
   · any-pointer: fine  → 同时存在鼠标/触控板（触屏笔记本）→ 仍按桌面处理
   模块顶层把判定结果写成 html[data-input-mode]，
   供 CSS 切换输入提示文案（index.html 的 .hint-keyboard/.hint-touch）。
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

var COARSE_QUERY = '(pointer: coarse)';
var FINE_QUERY = '(any-pointer: fine)';

function mql(query) {
  try { return window.matchMedia(query); } catch (e) { return null; }
}

/** true = 触屏输入环境（Enter 换行）；false = 有实体键盘（Enter 发送） */
export function isTouchUI() {
  var coarse = mql(COARSE_QUERY);
  if (!coarse || !coarse.matches) return false;
  var fine = mql(FINE_QUERY);
  return !(fine && fine.matches);
}

/** 把当前环境写进 html[data-input-mode]，CSS 据此二选一显示提示 */
function sync() {
  var root = document.documentElement;
  if (!root) return;
  root.setAttribute('data-input-mode', isTouchUI() ? 'touch' : 'keyboard');
}

sync();
// 外接/拔出键盘、旋转设备、桌面浏览器开设备模拟 → 提示文案跟着变
[COARSE_QUERY, FINE_QUERY].forEach(function (q) {
  var m = mql(q);
  if (m && m.addEventListener) m.addEventListener('change', sync);
});
