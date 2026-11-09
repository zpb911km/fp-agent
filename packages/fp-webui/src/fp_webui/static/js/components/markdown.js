/* ═══════════════════════════════════════════════════════════
   FP WebUI — Markdown 渲染
   文件引用链接 + marked/DOMPurify/katex/mermaid 容器（顶层挂 document click 监听）
   从 ui.js 按职责拆分（组件模块化），2026-11-08
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { escapeHtml } from './util.js';
import { getToken } from '../core/http.js';

// ── 消息渲染 ──

// ── 文件引用渲染：[file:inbox/name] / [file:outbox/name] → 下载链接 ──
// 协议相对格式（跨平台稳定），物理路径由服务端按本机集散点解析。
export function renderFileRefs(html) {
  return html.replace(/\[file:(inbox|outbox)\/([^\]\r\n]+?)\]/g, function (_, box, name) {
    // 下载走 JS fetch + Authorization 头（token 不进 URL/历史/Referer），
    // 成功后用 blob + a[download] 触发保存
    return '<a class="file-ref" href="#" data-filebox="' + escapeHtml(box) + '" data-filename="' + escapeHtml(name) + '" title="下载 ' + escapeHtml(name) + '">' +
           '📄 ' + escapeHtml(name) + '</a>';
  });
}

async function handleFileRefClick(ev) {
  var a = ev.target.closest('a.file-ref');
  if (!a) return;
  ev.preventDefault();
  var box = a.getAttribute('data-filebox');
  var name = a.getAttribute('data-filename');
  try {
    var resp = await fetch('/api/files/download?box=' + encodeURIComponent(box) +
                           '&name=' + encodeURIComponent(name), {
      headers: { 'Authorization': 'Bearer ' + getToken() }
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    var blob = await resp.blob();
    var url = URL.createObjectURL(blob);
    var tmp = document.createElement('a');
    tmp.href = url;
    tmp.download = name;
    document.body.appendChild(tmp);
    tmp.click();
    tmp.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 5000);
  } catch (e) {
    markMissing(a, e.message);
  }
}

function markMissing(a, msg) {
  // 404 = 集散点里已无此文件（被覆盖/清理）；其它错误也给出反馈
  if (String(msg).indexOf('404') !== -1) {
    a.classList.add('file-ref-missing');
    a.removeAttribute('data-filebox');
    a.title = '文件已不存在（可能被覆盖或清理）';
    a.innerHTML = '⚠️ ' + a.getAttribute('data-filename') + '（已不存在）';
  } else {
    a.title = '下载失败：' + msg;
    a.classList.add('file-ref-error');
    setTimeout(function () { a.classList.remove('file-ref-error'); }, 2000);
  }
}
document.addEventListener('click', handleFileRefClick);

export function renderMarkdown(content) {
  return renderFileRefs(_renderMarkdownBase(content));
}

function _renderMarkdownBase(content) {
  if (!content) return '';
  if (typeof marked === 'undefined') {
    return escapeHtml(content).replace(/\n/g, '<br>');
  }
  try {
    var html = marked.parse(content, { breaks: true, gfm: true });
    // DOMPurify 清洗 HTML，移除恶意标签/属性
    if (typeof DOMPurify !== 'undefined') {
      html = DOMPurify.sanitize(html);
    } else {
      // DOMPurify 不可用时，降级到 escapeHtml（牺牲格式保安全）
      return escapeHtml(content).replace(/\n/g, '<br>');
    }
    // 包裹所有 <table> 使其可横向滚动
    html = html.replace(/<table>/g, '<div class="table-wrapper"><table>');
    html = html.replace(/<\/table>/g, '</table></div>');
    // mermaid 代码块 → 图形容器（交由 mermaid.js 渲染；src 已由 marked 转义，安全）
    html = html.replace(
      /<pre><code class="language-mermaid">([\s\S]*?)<\/code><\/pre>/g,
      function (_, src) { return '<div class="mermaid">' + src + '</div>'; }
    );
    return html;
  } catch (e) {
    return escapeHtml(content);
  }
}

export function renderMath(root) {
  if (typeof renderMathInElement === 'undefined') return;
  try {
    renderMathInElement(root || document, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '$', right: '$', display: false }
      ],
      throwOnError: false,
      macros: { '\\R': '\\mathbb{R}', '\\N': '\\mathbb{N}', '\\Z': '\\mathbb{Z}' }
    });
  } catch (e) {}
}

