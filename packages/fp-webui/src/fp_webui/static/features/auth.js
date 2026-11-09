/* ═══════════════════════════════════════════════════════════
   FP WebUI — 登录（遮罩/token/初始化触发）
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { setToken } from '../core/http.js';
import { initApp } from './bootstrap.js';

// ── 登录 ──
export function showLogin() {
  document.getElementById('authOverlay').style.display = 'flex';
  document.getElementById('authInput').value = '';
  document.getElementById('authError').classList.remove('show');
  document.getElementById('authBtn').disabled = false;
  setTimeout(function() { document.getElementById('authInput').focus(); }, 100);
}

export function hideLogin() {
  document.getElementById('authOverlay').style.display = 'none';
}

export async function doLogin() {
  var token = document.getElementById('authInput').value.trim();
  if (!token) return;
  var btn = document.getElementById('authBtn');
  var err = document.getElementById('authError');
  btn.disabled = true;
  err.classList.remove('show');
  try {
    var res = await fetch('/api/auth', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: token })
    });
    if (res.ok) {
      setToken(token);
      hideLogin();
      initApp();
    } else {
      err.textContent = '⛔ Token 无效，请重试';
      err.classList.add('show');
    }
  } catch (e) {
    err.textContent = '❌ 服务器连接失败';
    err.classList.add('show');
  }
  btn.disabled = false;
}