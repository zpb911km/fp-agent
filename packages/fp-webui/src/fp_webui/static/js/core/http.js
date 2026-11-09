/* ═══════════════════════════════════════════════════════════
   FP WebUI — 认证与带鉴权的请求
   从 ui.js（认证四件套 + AUTH_KEY）与 app.js（withAuth/authFetch）
   下沉，用于消除 ui → app 的反向依赖（原仅 3 个符号，见
   ESM_AUDIT.md §3.2 / §7 循环依赖解法 1）。
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

export const AUTH_KEY = 'webui_token';

export function getToken() {
  return sessionStorage.getItem(AUTH_KEY) || '';
}

export function setToken(t) {
  sessionStorage.setItem(AUTH_KEY, t);
}

export function clearToken() {
  sessionStorage.removeItem(AUTH_KEY);
}

export function isAuthed() {
  return !!getToken();
}

export function withAuth(opts) {
  opts = opts || {};
  opts.headers = opts.headers || {};
  opts.headers['Authorization'] = 'Bearer ' + getToken();
  return opts;
}

/* 401 时的 UI 回调（原为 app.js 的 showLogin）。
   core 不许反向依赖 app，故由 app.js 启动时注入。 */
let _onUnauthorized = null;

export function setOnUnauthorized(fn) {
  _onUnauthorized = fn;
}

export async function authFetch(url, opts) {
  var res = await fetch(url, withAuth(opts));
  if (res.status === 401) {
    clearToken();
    if (_onUnauthorized) _onUnauthorized();
    throw new Error('未授权');
  }
  return res;
}
