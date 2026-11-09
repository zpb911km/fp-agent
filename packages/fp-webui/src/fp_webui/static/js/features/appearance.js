/* ═══════════════════════════════════════════════════════════
   FP WebUI — 外观（主题/光环开关，localStorage 持久化）
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

// 本模块零外部依赖：只碰 document/localStorage/window。
// ── 主题切换 ──
export function applyTheme(isLight) {
  var root = document.documentElement;
  var btn = document.getElementById('themeBtn');
  if (isLight) {
    root.classList.add('light-theme');
    if (btn) btn.innerHTML = '<span aria-hidden="true">🌙</span>';
    localStorage.setItem('webui_theme', 'light');
  } else {
    root.classList.remove('light-theme');
    if (btn) btn.innerHTML = '<span aria-hidden="true">☀️</span>';
    localStorage.setItem('webui_theme', 'dark');
  }
}

export function toggleTheme() {
  var root = document.documentElement;
  applyTheme(!root.classList.contains('light-theme'));
}

// ── 光环背景开关（可选装饰，默认关闭以省 GPU）──
function syncHaloBtn(on) {
  var btn = document.getElementById('haloBtn');
  if (!btn) return;
  btn.innerHTML = '<span aria-hidden="true">' + (on ? '✨' : '💤') + '</span>';
  btn.title = on ? '光环背景：开（点击关闭以节省 GPU）' : '光环背景：关（点击开启，较耗 GPU）';
}

export function applyHalo(on) {
  if (typeof window.setHaloEnabled === 'function') {
    on = window.setHaloEnabled(on);
  } else {
    // ui.js 兜底：直接隐藏元素
    var haloEl = document.getElementById('halo-bg');
    if (haloEl) haloEl.style.display = on ? '' : 'none';
    on = !!on;
  }
  try { localStorage.setItem('webui_halo', on ? 'on' : 'off'); } catch (e) {}
  syncHaloBtn(on);
  return on;
}

export function toggleHalo() {
  var on = (typeof window.isHaloEnabled === 'function')
    ? window.isHaloEnabled()
    : (localStorage.getItem('webui_halo') === 'on');
  applyHalo(!on);
}