/* ═══════════════════════════════════════════════════════════
   FP WebUI — 文件上传（集散点 inbox：选中/粘贴直传 + 待发标记）
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { escapeHtml, showError, inputEl } from '../ui.js';
import { authFetch } from '../core/http.js';

// ── 文件上传（集散点 inbox） ──
// 选中即后台上传，输入框显示可删除标记；发送时把 [filehub:inbox/name]
// 引用拼进消息正文，agent 收到路径引用即可取用。
var _pendingFiles = [];  // [{name, path, size}]

export function onFileInputChange(ev) {
  var files = ev.target.files || [];
  for (var i = 0; i < files.length; i++) uploadFile(files[i]);
  ev.target.value = '';  // 允许重复选择同一文件
}

/* 上传一个文件到 inbox。返回 Promise：成功 resolve 服务端响应（含 ref），
   失败 resolve null —— **不 reject**，因为 UI 主路径（选文件）不关心返回值，
   reject 会变成 unhandledrejection 噪声；诊断模块（core/diag）靠 null 判定失败。 */
export function uploadFile(file) {
  var fd = new FormData();
  fd.append('file', file, file.name);
  // withAuth 只加 Authorization；multipart 不能手动设 content-type
  return authFetch('/api/files', { method: 'POST', body: fd })
    .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
    .then(function (res) {
      if (!res.ok) { showError(res.d.detail || '上传失败'); return null; }
      _pendingFiles.push(res.d);
      renderPendingFiles();
      return res.d;
    })
    .catch(function (e) {
      if (e.message !== '未授权') showError('上传失败: ' + e.message);
      return null;
    });
}

function removePendingFile(idx) {
  // 标记可删，已落盘的副本留在集散点不回收（都是副本，宁可多占不误删）
  _pendingFiles.splice(idx, 1);
  renderPendingFiles();
}

function renderPendingFiles() {
  var bar = document.getElementById('pendingFiles');
  if (!_pendingFiles.length) { bar.hidden = true; bar.innerHTML = ''; return; }
  bar.hidden = false;
  bar.innerHTML = _pendingFiles.map(function (f, i) {
    return '<span class="pending-file" title="' + escapeHtml(f.path) + '">' +
      '📄 ' + escapeHtml(f.name) +
      ' <button class="pending-file-x" data-idx="' + i + '" ' +
      'aria-label="移除 ' + escapeHtml(f.name) + '">×</button></span>';
  }).join('');
}

// 粘贴截图/文件直传（截图场景主通道）
inputEl.addEventListener('paste', function (ev) {
  var items = (ev.clipboardData && ev.clipboardData.items) || [];
  var picked = false;
  for (var i = 0; i < items.length; i++) {
    if (items[i].kind === 'file') {
      var f = items[i].getAsFile();
      if (f) { uploadFile(f); picked = true; }
    }
  }
  if (picked) ev.preventDefault();
});
/* ── 移除按钮：事件委托（原内联 onclick 在全局作用域求值，
   ESM 化后 removePendingFile 不再是 window 符号 → 点击即 ReferenceError。
   改为容器委托：容器静态存在于 index.html，innerHTML 重绘不掉监听。）── */
document.getElementById('pendingFiles').addEventListener('click', function (e) {
  var btn = e.target.closest('.pending-file-x');
  if (!btn) return;
  removePendingFile(parseInt(btn.dataset.idx, 10));
});

/* ── compose.js 的待发文件出口 ──────────────────────────────
   原 _pendingFiles 是 app.js 模块局部变量，sendMessage 直接读写。
   拆分后不可跨模块触碰私有状态 → 收敛为一对动词：
   hasPendingFiles() 判存在，consumePendingRefs() 取引用串并清空重渲染。 */
export function hasPendingFiles() {
  return _pendingFiles.length > 0;
}

export function consumePendingRefs() {
  if (!_pendingFiles.length) return '';
  var refs = _pendingFiles.map(function (f) { return f.ref; }).join(' ');
  _pendingFiles = [];
  renderPendingFiles();
  return refs;
}
