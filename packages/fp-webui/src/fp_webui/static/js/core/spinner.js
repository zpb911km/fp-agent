/* ═══════════════════════════════════════════════════════════
   FP WebUI — Loading/Spinner 状态模块（三场景 loading 指示）
   ───────────────────────────────────────────────────────────
   场景① 打开会话列表首次拉取 → LoadingState.show('#sessionsList', …)
   场景② WS 重连过程           → LoadingState.show('#connectionStatus', …)
   场景③ 刷新页面初始加载       → 页面级 #fpPageLoading（本模块顶层接管）
   排除：对话流式输出 / 工具执行等待（不在本模块职责内）。

   本模块零依赖（不 import 任何 ui/core 模块），可被 features/* 单向引入，
   亦经 index.html <script type="module"> 独立执行以尽早接管首屏。
   样式全部在 css/spinner.css；颜色/时长一律 token（见契约披露）。
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

/** 行内占位节点：.fp-loading[data-fp-loading] > i.fp-spin-box + span.fp-loading-label */
function buildInline(label) {
  var box = document.createElement('span');
  box.className = 'fp-loading';
  box.setAttribute('data-fp-loading', '');
  box.setAttribute('role', 'status');
  var spin = document.createElement('i');
  spin.className = 'fp-spin-box';
  spin.setAttribute('aria-hidden', 'true');
  var text = document.createElement('span');
  text.className = 'fp-loading-label';
  text.textContent = label || '加载中…';
  box.appendChild(spin);
  box.appendChild(text);
  return box;
}

/** target：Element | 选择器字符串（'…' 传给 querySelector）；解析失败返回 null */
function resolve(target) {
  if (!target) return null;
  if (typeof target === 'string') {
    try { return document.querySelector(target); } catch (e) { return null; }
  }
  return target;
}

export const LoadingState = {
  /**
   * 在 target 首部显示行内 loading（幂等：已存在则仅更新 label）。
   * target 内容若被 innerHTML 整体替换，占位随之消失，hide() 幂等兜底。
   */
  show: function (target, label) {
    var el = resolve(target);
    if (!el) return;
    var exist = el.querySelector(':scope > [data-fp-loading]');
    if (exist) {
      if (label) {
        var lab = exist.querySelector('.fp-loading-label');
        if (lab) lab.textContent = label;
      }
      return;
    }
    el.prepend(buildInline(label));
  },

  /** 移除 target 内行内 loading；不存在时静默（幂等） */
  hide: function (target) {
    var el = resolve(target);
    if (!el) return;
    var exist = el.querySelector(':scope > [data-fp-loading]');
    if (exist) exist.remove();
  },

  /** 页面级占位：创建/显示 #fpPageLoading，并以 body.fp-js 关闭 CSS-only 伪元素层 */
  showPage: function (label) {
    if (!document.body) return;
    var ov = document.getElementById('fpPageLoading');
    if (!ov) {
      ov = document.createElement('div');
      ov.id = 'fpPageLoading';
      ov.setAttribute('role', 'status');
      ov.innerHTML = '<i class="fp-spin-box" aria-hidden="true"></i>' +
                     '<span class="fp-page-label"></span>';
      document.body.appendChild(ov);
    }
    if (label) {
      var lab = ov.querySelector('.fp-page-label');
      if (lab) lab.textContent = label;
    }
    ov.classList.remove('fp-page-done');
    document.body.classList.add('fp-js');   // 真元素接管，关 body::before/after
  },

  /** 隐藏页面级占位：0.15s 淡出后移除节点；未显示过则静默（幂等） */
  hidePage: function () {
    if (document.body) document.body.classList.add('fp-js');
    var ov = document.getElementById('fpPageLoading');
    if (!ov || ov.classList.contains('fp-page-done')) return;
    ov.classList.add('fp-page-done');
    setTimeout(function () {
      if (ov.parentNode) ov.remove();
    }, 200);
  }
};

/* 全局挂载：与 index.html 内联事件/测试接缝同语义的兜底出口 */
window.LoadingState = LoadingState;

/* 模块就绪即接管首屏（type=module 天然 defer，DOM 已解析）——
   此后 hidePage 的唯一时机是 ws.onopen（场景③验收点） */
if (document.body) {
  LoadingState.showPage('正在启动…');
} else {
  document.addEventListener('DOMContentLoaded', function () {
    LoadingState.showPage('正在启动…');
  });
}
