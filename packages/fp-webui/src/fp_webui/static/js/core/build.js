/* ═══════════════════════════════════════════════════════════
   FP WebUI — 前端构建戳（老模块可见性）
   ───────────────────────────────────────────────────────────
   为什么需要它：前端是 ESM import 图，模块 URL 是写死的相对路径，
   改文件不改 URL；`Cache-Control: no-cache` 也只在**重新导航**时生效。
   agent 热重启、WS 断线重连、手机端 bfcache 回前台——都不重新下载模块。
   于是"后端已重载、前端也改过了，界面却还是老行为"极易被误判成"没改好"
   （已反复发生多轮：横幅早就删了，用户页面上它还在）。

   做法：两个信号各自与"本页加载时的值"比对，任一变化即露刷新入口。
     ① /api/build（整棵 static 树的最大 mtime）——精确，但要求服务端是新代码；
     ② index.html 里的 ?_t= 版本串（服务端按文件 mtime 改写）——**不依赖
        任何新端点**，老服务端也认。服务端进程跑着旧 Python（/api/build 404
        或 401）时，① 静默失效，② 仍能把"前端已过期"讲出来。
   信号取不到就当没有信息（绝不因为探测失败而报警：那会变成常驻噪声）。
   它只在**确实过期**时出现，且是唯一动作（reload），不重复讲连接态。
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

// 信号名 → 本页加载时读到的值。首次读到只立基准，之后变化才报警。
// 分信号记账（而不是一个合并值）：某一路探测失败不该污染另一路的判断。
var _base = {};

function hintEl() {
  return document.getElementById('buildHint');
}

function showHint() {
  var el = hintEl();
  if (el) el.hidden = false;
}

/** 登记一次探测读数：null = 取不到（不参与判断，也不立基准）。 */
function track(name, value) {
  if (value === null || value === undefined || value === '') return;
  if (!(name in _base)) { _base[name] = value; return; }   // 首次：只立基准
  if (_base[name] !== value) showHint();
}

// ── 信号②：本页资产版本 vs 磁盘资产版本 ─────────────────────
// 服务端把 script/link 上的 `?_t=` 改写成对应文件的 mtime，于是"本页这一套"
// 和"磁盘上那一套"可以逐条比对。整串拼起来比较（不逐条 diff）：只要有一条变，
// 就说明本页跑的模块不是磁盘上的最新版。

/** 从页面 DOM 取本页实际加载的版本串（它启动时下载的就是这一套）。 */
function pageAssetSig() {
  var pairs = [];
  var els = document.querySelectorAll('script[src], link[href]');
  for (var i = 0; i < els.length; i++) {
    var u = els[i].getAttribute('src') || els[i].getAttribute('href') || '';
    var m = u.match(/^(.*?)\?_t=(\d+)/);
    if (m) pairs.push(m[1] + ':' + m[2]);
  }
  return pairs.sort().join('|');
}

/** 重新取一次 index.html（绕过缓存），解析同一批 ?_t= 版本串。 */
function diskAssetSig() {
  return fetch('/', { cache: 'no-store' })
    .then(function (r) { return r.ok ? r.text() : null; })
    .then(function (html) {
      if (!html) return null;
      var pairs = [];
      var re = /(?:src|href)="([^"]*?)\?_t=(\d+)"/g;
      var m;
      while ((m = re.exec(html)) !== null) pairs.push(m[1] + ':' + m[2]);
      return pairs.length ? pairs.sort().join('|') : null;
    })
    .catch(function () { return null; });
}

// ── 信号①：/api/build ─────────────────────────────────────
// 刻意用裸 fetch：/api/build 在认证白名单里——它必须在**登录前**就能取到，
// 那一刻正是本页的基准戳（登录后再取，会把"本页已过期"误当成"刚刚好"）。
function apiStamp() {
  return fetch('/api/build')
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) {
      return d && typeof d.stamp === 'number' && d.stamp ? d.stamp : null;
    })
    .catch(function () { return null; });
}

/** 取一轮戳并与基准比对（幂等，可反复调用：启动一次、每次连上再一次）。 */
export function checkBuild() {
  apiStamp().then(function (v) { track('api', v); });
  if (!document.querySelector('script[src*="_t="], link[href*="_t="]')) return;  // 老服务端没改写：无此信号
  diskAssetSig().then(function (sig) {
    // 两侧用同一套算法成串才可比：DOM 侧在本页加载时就已固定（不再变动），
    // 这里每轮重算磁盘侧，两者不等 = 磁盘上的前端比本页新。
    var mine = pageAssetSig();
    if (sig && mine && sig !== mine) showHint();
  });
}

/** 绑定刷新入口（点击 = 重新导航 → 重新下载模块）。 */
export function initBuildHint() {
  var el = hintEl();
  if (el && !el.dataset.bound) {
    el.dataset.bound = '1';
    el.onclick = function () { location.reload(); };
  }
  checkBuild();
}
