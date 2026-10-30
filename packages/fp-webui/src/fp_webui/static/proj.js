/* ═══════════════════════════════════════════════════════════════════
   投影层 (proj.js)
   ───────────────────────────────────────────────────────────────────
   halo 不是贴在页面背后的背景图，而是一台**在屏幕前方**的投影仪投出的
   影像，落在 UI 各自的表面上：顶栏、输入区、每一条消息。

   光轴斜置：投影仪假设在「无限远的右上方」，所以光线是**平行光**。
   平行光的后果是——所有平面上的影像**大小与清晰度完全相同**，
   只有位置不同：

       u = x + z · tan(YAW)      近处偏右
       v = y − z · tan(PITCH)    近处偏上

   z 是该表面相对基准面的高度（px）。同一道弧线落在不同高度的表面上会
   错开，错开量只由 z 决定，与滚动位置无关 —— 这正是「前后错落」的来源。

   合成：整层 canvas 用 mix-blend-mode: screen 叠在页面之上，
   pointer-events: none，不拦截任何交互。
   ═══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  /* ── 可调参数 ─────────────────────────────────────── */
  var SHIFT   = 0.70;    // 投影强度
  var FALLOFF = 0;       // 大气透视：0=严格等亮（平行光应如此）
  var GRIME   = 0.65;    // 斑驳锈蚀强度：0=干净的光，1=满是锈孔
  var YAW     = 2;       // 水平倾角 → 横向错位 = z·tan(YAW)
  var PITCH   = 2;       // 竖直倾角 → 纵向错位 = z·tan(PITCH)

  var KX = Math.tan(YAW * Math.PI / 180);
  var KY = Math.tan(PITCH * Math.PI / 180);

  /* 各表面的高度（px）。z 越大 = 越靠近投影仪。 */
  var TOPBAR_Z   =   0;
  var COMPOSER_Z =  32;
  var WALL_Z     = -280;    // 消息区底板：全场最远的一层墙
  var WALL_ALPHA = 0.20;    // 底板整体不透明度（0.2，比消息层暗得多才不抢）
  var GLOW_DOWN  = 24;      // 光晕层降采样倍数（≈ 等效模糊半径 px）
  var WALL_DOWN  = 6;       // 本体层降采样倍数
  var GLOW_RATIO = 0.55;    // 光晕层占总亮度的比例
  var CARD_ZS    = [72, 0, -72];
  var CARD_R     = 8;

  /* 斑驳噪点阈值：三尺度（96/22/8px）叠加后分两档。
     调低 = 缺口更少更零星；想「大而不多」应加大上面的 cell，而非压阈值。 */
  var THR_HARD = 0.250;   // 低于此 → alpha 255，真挖穿
  var THR_SOFT = 0.360;   // 低于此 → alpha 110，只削薄

  function opFor(z) {
    return Math.max(0, SHIFT - FALLOFF * Math.min(1, Math.max(0, -z) / 280));
  }

  /* ── DOM ──────────────────────────────────────────── */
  var haloCanvas = document.getElementById('halo-canvas');
  var ov         = document.getElementById('proj-layer');
  var msgBox     = document.getElementById('messagesContainer');
  var topbar     = document.querySelector('.topbar');
  var composer   = document.querySelector('.input-area');
  var overlayEl  = document.getElementById('authOverlay');
  var sessionsModal = document.getElementById('sessionsModal');
  if (!haloCanvas || !ov || !msgBox) return;

  var ovctx = ov.getContext('2d');

  /* ── 斑驳锈蚀遮罩 ──────────────────────────────────
     屏幕空间静态纹理，只在视口/DPR 变化时重建一次。
     alpha=255 的像素真正挖穿投影（透出底下的界面），alpha=110 只削薄。 */
  var grimeCv = null, grimeW = 0, grimeH = 0;

  function randField(cols, rows) {
    var a = new Float32Array(cols * rows);
    for (var i = 0; i < a.length; i++) a[i] = Math.random();
    return { a: a, cols: cols, rows: rows };
  }

  /* 双线性：大尺度用，等值线平滑，缺口边界自然 */
  function sampBilinear(f, u, v) {
    var fx = u * (f.cols - 1), fy = v * (f.rows - 1);
    if (fx < 0) fx = 0; if (fy < 0) fy = 0;
    var x0 = fx | 0, y0 = fy | 0;
    if (x0 > f.cols - 2) x0 = Math.max(0, f.cols - 2);
    if (y0 > f.rows - 2) y0 = Math.max(0, f.rows - 2);
    var x1 = Math.min(x0 + 1, f.cols - 1), y1 = Math.min(y0 + 1, f.rows - 1);
    var tx = fx - x0, ty = fy - y0, a = f.a, c = f.cols;
    var r0 = a[y0 * c + x0] * (1 - tx) + a[y0 * c + x1] * tx;
    var r1 = a[y1 * c + x0] * (1 - tx) + a[y1 * c + x1] * tx;
    return r0 * (1 - ty) + r1 * ty;
  }

  /* 最近邻：中小尺度用，格子边界直接形成方块阶梯 = 崩边锯齿 */
  function sampNearest(f, u, v) {
    var x = (u * f.cols) | 0, y = (v * f.rows) | 0;
    if (x >= f.cols) x = f.cols - 1;
    if (y >= f.rows) y = f.rows - 1;
    return f.a[y * f.cols + x];
  }

  /* 三尺度叠加：96px 整块缺损 + 22px 斑驳 + 8px 麻点。
     只用单一尺度的后果是所有孔洞一边大，看着像均匀噪点而非锈蚀。 */
  function buildGrime(w, h) {
    var f1 = randField(Math.ceil(w / 96) + 2, Math.ceil(h / 96) + 2);
    var f2 = randField(Math.ceil(w / 22) + 2, Math.ceil(h / 22) + 2);
    var f3 = randField(Math.ceil(w /  8) + 2, Math.ceil(h /  8) + 2);

    var c = document.createElement('canvas');
    c.width = w; c.height = h;
    var g = c.getContext('2d');
    var id = g.createImageData(w, h), d = id.data;

    for (var y = 0; y < h; y++) {
      var v = y / h, o = y * w * 4;
      for (var x = 0; x < w; x++) {
        var u = x / w;
        var n = 0.44 * sampBilinear(f1, u, v)
              + 0.32 * sampNearest(f2, u, v)
              + 0.24 * sampNearest(f3, u, v);
        d[o + 3] = n < THR_HARD ? 255 : (n < THR_SOFT ? 110 : 0);
        o += 4;
      }
    }
    g.putImageData(id, 0, 0);
    return c;
  }

  function ensureGrime(w, h) {
    if (grimeCv && grimeW === w && grimeH === h) return;
    grimeW = w; grimeH = h;
    grimeCv = buildGrime(w, h);
  }

  /* 只啃形状，不碰颜色。×1.45 让 GRIME≈0.69 时 255 的孔真正挖穿。 */
  function applyGrime(ctx, vw, vh) {
    if (GRIME <= 0.002 || !grimeCv) return;
    ctx.save();
    ctx.globalCompositeOperation = 'destination-out';
    ctx.globalAlpha = Math.min(1, GRIME * 1.45);
    ctx.drawImage(grimeCv, 0, 0, vw, vh);
    ctx.restore();
  }

  /* ── 取样 ──────────────────────────────────────────
     从 halo 画布 1:1 取一块贴到目标矩形上。源矩形越界时按比例裁剪
     目标，避免 drawImage 拉伸失真。 */
  function blit(ctx, sx, sy, sw, sh, dx, dy, dw, dh, alpha) {
    var x0 = sx < 0 ? 0 : sx,
        y0 = sy < 0 ? 0 : sy,
        x1 = Math.min(haloCanvas.width,  sx + sw),
        y1 = Math.min(haloCanvas.height, sy + sh);
    if (x1 <= x0 || y1 <= y0) return;
    var kx = dw / sw, ky = dh / sh;
    ctx.globalAlpha = alpha;
    ctx.drawImage(
      haloCanvas,
      x0, y0, x1 - x0, y1 - y0,
      dx + (x0 - sx) * kx, dy + (y0 - sy) * ky,
      (x1 - x0) * kx, (y1 - y0) * ky
    );
  }

  /* ── 柔和取样（降采样近似模糊）──────────────────────
     把源矩形缩到 1/k，再放大回目标尺寸；两次双线性插值等效于
     k px 半径的柔化，成本和 k 几乎无关（真正的 blur 是 O(半径)）。
     dsCv 只增不减，避免每帧重建画布。 */
  var dsCv = null, dsCtx = null;

  function softBlit(ctx, sx, sy, sw, sh, dx, dy, dw, dh, alpha, k) {
    k = Math.max(2, Math.round(k));
    var x0 = sx < 0 ? 0 : sx,
        y0 = sy < 0 ? 0 : sy,
        x1 = Math.min(haloCanvas.width,  sx + sw),
        y1 = Math.min(haloCanvas.height, sy + sh);
    if (x1 <= x0 || y1 <= y0) return;
    var kx = dw / sw, ky = dh / sh;
    var tx = dx + (x0 - sx) * kx, ty = dy + (y0 - sy) * ky,
        tw = (x1 - x0) * kx,      th = (y1 - y0) * ky;

    var lw = Math.max(1, Math.round(tw / k)), lh = Math.max(1, Math.round(th / k));
    if (!dsCv) { dsCv = document.createElement('canvas'); dsCtx = dsCv.getContext('2d'); }
    if (dsCv.width < lw || dsCv.height < lh) {
      dsCv.width  = Math.max(dsCv.width,  lw);
      dsCv.height = Math.max(dsCv.height, lh);
    }
    dsCtx.setTransform(1, 0, 0, 1, 0, 0);
    dsCtx.globalCompositeOperation = 'copy';
    dsCtx.imageSmoothingEnabled = true;
    dsCtx.imageSmoothingQuality = 'low';
    dsCtx.drawImage(haloCanvas, x0, y0, x1 - x0, y1 - y0, 0, 0, lw, lh);

    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'low';
    ctx.drawImage(dsCv, 0, 0, lw, lh, tx, ty, tw, th);
    ctx.restore();
  }

  function clipRound(ctx, x, y, w, h, r) {
    ctx.beginPath();
    if (ctx.roundRect) { ctx.roundRect(x, y, w, h, r); }
    else {
      r = Math.min(r, w / 2, h / 2);
      ctx.moveTo(x + r, y);
      ctx.arcTo(x + w, y,     x + w, y + h, r);
      ctx.arcTo(x + w, y + h, x,     y + h, r);
      ctx.arcTo(x,     y + h, x,     y,     r);
      ctx.arcTo(x,     y,     x + w, y,     r);
      ctx.closePath();
    }
    ctx.clip();
  }

  /* ── 接收面登记 ────────────────────────────────────
     消息是动态增删的，用 IntersectionObserver 只保留**视口内**的那些。
     直接遍历全部 .msg 逐个 getBoundingClientRect 会在长会话里拖垮滚动。 */
  var zMap = new WeakMap();     // el -> 该消息的高度
  var visMsgs = new Set();      // 当前视口内的消息
  var zSeq = 0;

  var io = new IntersectionObserver(function (ents) {
    for (var i = 0; i < ents.length; i++) {
      var e = ents[i];
      if (e.isIntersecting) visMsgs.add(e.target);
      else visMsgs.delete(e.target);
    }
    markDirty();
  }, { root: msgBox, rootMargin: '160px 0px' });

  function observeMsg(el) {
    if (zMap.has(el)) return;
    zMap.set(el, CARD_ZS[zSeq++ % CARD_ZS.length]);
    io.observe(el);
  }

  function scanAll() {
    var list = msgBox.querySelectorAll('.msg');
    for (var i = 0; i < list.length; i++) observeMsg(list[i]);
  }

  var mo = new MutationObserver(function (recs) {
    for (var i = 0; i < recs.length; i++) {
      var added = recs[i].addedNodes;
      for (var j = 0; j < added.length; j++) {
        var n = added[j];
        if (n.nodeType !== 1) continue;
        if (n.classList && n.classList.contains('msg')) observeMsg(n);
        else if (n.querySelectorAll) {
          var sub = n.querySelectorAll('.msg');
          for (var k = 0; k < sub.length; k++) observeMsg(sub[k]);
        }
      }
    }
    markDirty();
  });
  mo.observe(msgBox, { childList: true, subtree: true });

  scanAll();

  /* ── 绘制 ──────────────────────────────────────────── */
  var lastDraw = 0, lastDirty = -1e9;
  var drawMs = 0, fps = 0, fpsN = 0, fpsT = 0;

  function markDirty() { lastDirty = performance.now(); }

  /* 投影层是否已是「干净」状态。早退时必须清空画布，
     否则上一帧的光会僵在屏幕上（弹窗打开后仍残留投影）。 */
  var ovClean = false;
  function clearOv() {
    if (ovClean) return;
    ovctx.setTransform(1, 0, 0, 1, 0, 0);
    ovctx.clearRect(0, 0, ov.width, ov.height);
    ovClean = true;
  }

  function drawAll() {
    // halo 没启用 / 没在跑 → 没有光可投
    // 登录遮罩期间不投：那层是模态的，底下界面不可交互。
    // 注意 app.js 用的是 style.display 切换（不是 class）。
    // 会话弹窗同理：它的 z-index(100) 低于投影层(9998)，不排除的话
    // 光会盖在模态面板上。每帧检查，关闭后自动恢复。
    if (!window.__haloLight || !haloCanvas.width
        || (window.isHaloEnabled && !window.isHaloEnabled())
        || (overlayEl && overlayEl.style.display !== 'none')
        || (sessionsModal && sessionsModal.classList.contains('active'))) {
      clearOv();
      return;
    }

    var vw = window.innerWidth, vh = window.innerHeight;
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var mdpr = haloCanvas.width / (haloCanvas.clientWidth || vw);

    var ow = Math.round(vw * dpr), oh = Math.round(vh * dpr);
    if (ov.width !== ow || ov.height !== oh) {
      ov.width = ow; ov.height = oh;   // 重设尺寸会自动清空画布
      ovClean = false;
    }

    ovctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ovctx.clearRect(0, 0, vw, vh);
    ensureGrime(ov.width, ov.height);

    /* 批量读：把 getBoundingClientRect 全部收集完再统一绘制，
       避免「读一个画一个」反复触发样式重算。 */
    var list = [];
    function push(el, z, r, glow) {
      if (!el) return;
      var b = el.getBoundingClientRect();
      if (b.width <= 0 || b.height <= 0) return;
      if (b.bottom <= 0 || b.top >= vh || b.right <= 0 || b.left >= vw) return;
      list.push({ z: z, r: r, b: b, glow: !!glow });
    }

    /* 消息区底板：整面墙，z=−280，全场最远的一层。
       它单独走 glow 分支 —— 远处本来就该朦胧：大模糊当散在空间里的晕，
       小模糊当落在墙上的像；alpha 固定 WALL_ALPHA，不吃 opFor 的距离衰减，
       否则又会和消息层亮度撞在一起、读成第二圈光环。 */
    push(msgBox,   WALL_Z,     0, true);
    push(topbar,   TOPBAR_Z,   0);
    push(composer, COMPOSER_Z, 0);

    visMsgs.forEach(function (el) {
      var z = zMap.get(el);
      if (z === undefined) return;
      push(el, z, CARD_R);
    });

    /* 远→近绘制：重叠处亮度叠加，近处自然更亮 */
    list.sort(function (a, b) { return a.z - b.z; });

    for (var i = 0; i < list.length; i++) {
      var it = list[i], z = it.z, b = it.b;
      var op = it.glow ? WALL_ALPHA : opFor(z);
      if (op <= 0.002) continue;
      var sx = (b.left + z * KX) * mdpr, sy = (b.top - z * KY) * mdpr,
          sw = b.width * mdpr,         sh = b.height * mdpr;
      ovctx.save();
      if (it.r > 0) clipRound(ovctx, b.left, b.top, b.width, b.height, it.r);
      if (it.glow) {
        /* 先画大晕、再画本体：本体压在上面才不会被晕糊掉。
           不用 ctx.filter='blur(Npx)' —— 全屏大半径模糊每帧 10ms+，
           而且成本落在合成阶段，JS 的 drawMs 完全测不到（实测 fps 掉到 12）。
           改用降采样：缩到 1/24 再放大回来，双线性插值本身就是柔化。 */
        softBlit(ovctx, sx, sy, sw, sh, b.left, b.top, b.width, b.height,
                 op * GLOW_RATIO, GLOW_DOWN);
        softBlit(ovctx, sx, sy, sw, sh, b.left, b.top, b.width, b.height,
                 op * (1 - GLOW_RATIO), WALL_DOWN);
      } else {
        blit(ovctx, sx, sy, sw, sh, b.left, b.top, b.width, b.height, op);
      }
      ovctx.restore();
    }

    applyGrime(ovctx, vw, vh);
    ovClean = false;
  }

  /* ── 调度 ──────────────────────────────────────────
     滚动/新增消息期间每帧绘制（位置与滚动同帧，不拖影）；
     静止时降到 ~38Hz 省电。 */
  var lastScroll = -1e9;

  function markScroll() {
    lastScroll = performance.now();
    markDirty();
  }

  msgBox.addEventListener('scroll', markScroll, { passive: true });
  window.addEventListener('scroll', markScroll, { passive: true });
  window.addEventListener('resize', function () {
    markScroll();
    drawAll();
  }, { passive: true });

  function tick(now) {
    var moving = (now - lastScroll) < 160 || (now - lastDirty) < 160;
    var gap = moving ? 0 : 26;
    if (now - lastDraw >= gap) {
      lastDraw = now;
      var t0 = performance.now();
      drawAll();
      drawMs = drawMs * 0.8 + (performance.now() - t0) * 0.2;
    }
    fpsN++;
    if (now - fpsT > 500) { fps = Math.round(fpsN * 1000 / (now - fpsT)); fpsN = 0; fpsT = now; }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  /* ── 对外：给控制台留调参入口 ─────────────────────── */
  window.__proj = {
    set: function (o) {
      if (o.SHIFT   !== undefined) SHIFT   = o.SHIFT;
      if (o.FALLOFF !== undefined) FALLOFF = o.FALLOFF;
      if (o.GRIME   !== undefined) GRIME   = o.GRIME;
      if (o.YAW     !== undefined) { YAW = o.YAW;     KX = Math.tan(YAW * Math.PI / 180); }
      if (o.PITCH   !== undefined) { PITCH = o.PITCH; KY = Math.tan(PITCH * Math.PI / 180); }
      markScroll();
      return { SHIFT: SHIFT, FALLOFF: FALLOFF, GRIME: GRIME, YAW: YAW, PITCH: PITCH };
    },
    stats: function () { return { fps: fps, drawMs: +drawMs.toFixed(2), visible: visMsgs.size }; }
  };
})();
