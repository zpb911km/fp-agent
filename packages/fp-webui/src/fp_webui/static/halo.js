/* ═══════════════════════════════════════════════════════════════════════
   halo.js — 迭代器光环 #2139 · 状态机移植版

   来源：KDE 插件仓库 tools/halo2139_sim.py（Rain World OracleGraphics.Halo
   的逐参数复现，常量取自 Assembly-CSharp.dll 的 IL 反汇编与
   VectorCircle.shader 明文）。

   与旧版 SVG 装饰实现的本质区别：旧版是 5 个同心圆 + CSS keyframes，
   表现是"看起来像"；本版移植的是真正的状态机 ——
     · 2 个主环（radius = (3+i+push)·expand·10）
     · 100 个独立呼吸的 MemoryBit（10/30/60 三环）
     · 20 条贝塞尔连线（fireChance=rnd³，lightUp *= 0.9 衰减）
     · expand / push / white 三个受激发的全局状态量
     · 固定 40Hz tick，渲染端在两 tick 之间线性插值

   随机源是 mulberry32（可播种），与 sim.py 的 random 序列在
   "消耗顺序一致"的前提下逐帧可对齐。

   时间基准是**固定步长累加器**，不是 rAF 的 dt —— 掉帧只补 tick，
   不改变每个 tick 的推进量，因此行为与帧率无关。
   ═══════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var hasDoc = typeof document !== 'undefined';
  var haloEl = hasDoc ? document.getElementById('halo-bg') : null;
  var canvas = haloEl ? document.getElementById('halo-canvas') : null;
  var ctx = canvas ? canvas.getContext('2d', { alpha: true }) : null;
  var HAS_DOM = !!(haloEl && canvas && ctx);

  var TICK_HZ = 40;
  var TICK_MS = 1000 / TICK_HZ;
  var RING_BITS = [10, 30, 60];
  var WARMUP_TICKS = 3000;      // 75 秒预热：让所有 LerpAndTick 先收敛
  var MAX_CATCHUP_MS = 250;     // 单帧最多补 10 个 tick，防雪崩

  /* ─────────────────────────── 原语（对应 Custom:: 静态方法，命名一致以便回查） */

  function L(a, b, t) { return a + (b - a) * t; }

  // Custom.LerpAndTick：先按固定比例趋近，再按固定步长钳到目标
  function lat(cur, tgt, lerp, tick) {
    var r = L(cur, tgt, lerp);
    return r < tgt ? Math.min(r + tick, tgt) : Math.max(r - tick, tgt);
  }

  function scurve(t, k) {
    t = t * 2.0 - 1.0;
    if (t < 0) {
      t = Math.abs(1.0 + t);
      return 0.5 * ((k * t) / (1.0 + k - t));
    }
    k = -1.0 - k;
    return 0.5 + 0.5 * ((k * t) / (k - t + 1.0));
  }

  function deg2vec(d) {
    var r = d * Math.PI / 180;
    return [Math.cos(r), Math.sin(r)];
  }

  function dirvec(ax, ay, bx, by) {
    var dx = bx - ax, dy = by - ay, ln = Math.sqrt(dx * dx + dy * dy) || 1;
    return [dx / ln, dy / ln];
  }

  // 三次贝塞尔。注意 Custom.Bezier 的实参顺序与标准 P0..P3 不同：
  // IL 展开为 (1-t)³a + 3t(1-t)²b + 3t²(1-t)d + t³c，第 3、4 参数权重互换。
  // 所有调用点必须写成 bezier3(p0, p1, p3, p2, t) —— 见 sim.py 同名注释。
  function bezier3(p0, p1, p2, p3, t) {
    var u = 1 - t, a = u * u * u, b = 3 * u * u * t, c = 3 * u * t * t, d = t * t * t;
    return [a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
            a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]];
  }

  /* ─────────────────────────── 可播种 PRNG */

  function makeRng(seed) {
    var s = seed >>> 0;
    return function () {
      s = (s + 0x6D2B79F5) >>> 0;
      var t = s;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  /* 等价 Random.Range(int, int)：上界不含，恰好消耗一次随机数。
     Python 的 randrange 走 _randbelow(n)=getrandbits(bit_length) 重抽样，
     消耗个数是随机的（几何分布）——要与 sim.py 逐帧对齐时，注入的 rng 需自带
     randrange 方法走同一路径；浏览器默认走浮点乘法（快且无重抽样）。 */
  function randInt(rng, lo, hi) {
    if (typeof rng.randrange === 'function') return rng.randrange(lo, hi);
    return lo + Math.floor(rng() * (hi - lo));
  }

  /* ─────────────────────────── 状态对象 */

  function MemoryBit(rng) {
    this.filled = rng();
    this.lastFilled = this.filled;
    this.getToFilled = this.filled;
    this.fillSpeed = 0;
    this.blinkCounter = 0;
  }

  MemoryBit.prototype.update = function (rng) {
    this.lastFilled = this.filled;
    if (this.filled !== this.getToFilled) {
      this.filled = lat(this.filled, this.getToFilled, 0.03, this.fillSpeed);
    } else if (this.blinkCounter > 0) {
      this.blinkCounter--;
    } else if (rng() < 1 / 60) {
      this.getToFilled = rng();
      this.fillSpeed = 1.0 / L(2.0, 80.0, rng());
    }
  };

  // 整环脉冲：打满 + 加速 + 闪 20 帧
  MemoryBit.prototype.setToMax = function () {
    this.getToFilled = 1.0;
    this.fillSpeed = L(this.fillSpeed, 0.25, 0.25);
    this.blinkCounter = 20;
  };

  MemoryBit.prototype.fill = function (t) {
    // blinkCounter 的 4 相里只亮 2 帧
    if (this.blinkCounter % 4 > 1 && this.filled === this.getToFilled) return 0;
    return L(this.lastFilled, this.filled, t);
  };

  /* ── 连线几何：两种模式，由 CONN_MODE 选择 ──
     "game"  ：忠实 sim.py / IL 反汇编。锚点固定在 halo 外 300~500 游戏单位。
               在网页这种"halo 紧贴右上角"的容器里，zoom≈4.1 会把锚点推到
               1240~2067px 之外（画外 1~2 屏），于是整条线只剩中段可见，
               看起来像一根横穿画面的弦，而不是神经。
     "radial"：webui 适配版。从 halo 外缘沿固定角度射向视口 4 条边，
               整条线都在画面内 —— 神经从光环向屏幕外延伸。
     两种模式都在构造时消费固定数量的随机数，角度/弯曲仍可复现。 */
  var CONN_MODE = 'radial';

  function Connection(cx, cy, rng, idx, total) {
    this.lightUp = 0;
    this.lastLightUp = 0;
    if (CONN_MODE === 'radial') {
      /* 角度按 total 等分后加抖动 —— 纯随机会让某些方向长期无线，
         分层后 20 条能均匀覆盖视口四方，任何时刻亮的几条都分散。 */
      var slot = (idx + rng()) / total;
      this.angle = slot * Math.PI * 2;            // 射向哪个方向（→ 哪条边）
      this.bend = rng() * 2 - 1;                  // 弯曲方向（符号）+ 幅度
      this.taper = rng();                         // 末端渐隐强度
      this.stuckAt = null;                        // 端点位置在渲染时按视口求解
      this.handle = null;
      return;
    }
    /* —— 以下为 game 模式 ——
       handle 的基点是被夹之前的 pos，不是 stuckAt。
       armBox 为 null 时不做夹取（等价于假设锚点都落在手臂包围盒内）。
       原版包围盒由 OracleArm 逐帧算出，不属于 Halo 的 IL，故留作外部参数。 */
    var a = rng() * Math.PI * 2;                  // RNV()：第 1 个随机数
    var r = L(300.0, 500.0, rng());               // 第 2 个
    var pos = [cx + Math.cos(a) * r, cy + Math.sin(a) * r];
    this.stuckAt = pos;                           // armBox=null 时原样
    var ha = rng() * Math.PI * 2;                 // RNV()：第 3 个
    var hr = L(400.0, 700.0, rng());              // 第 4 个
    this.handle = [pos[0] + Math.cos(ha) * hr, pos[1] + Math.sin(ha) * hr];
  }

  function Halo(rng) {
    this.rng = rng;
    this.connections = [];
    for (var i = 0; i < 20; i++) this.connections.push(new Connection(0, 0, rng, i, 20));
    this.fireChance = Math.pow(rng(), 3);
    this.bits = [];
    for (var k = 0; k < RING_BITS.length; k++) {
      var ring = [];
      for (var j = 0; j < RING_BITS[k]; j++) ring.push(new MemoryBit(rng));
      this.bits.push(ring);
    }
    this.rot = [];                                // 10 环 × [cur,last,start,target,progress]
    for (var m = 0; m < 10; m++) this.rot.push([0, 0, 0, 0, 0]);
    this.expand = 1; this.lastExpand = 1; this.getToExpand = 1;
    this.push = 0; this.lastPush = 0; this.getToPush = 0;
    this.white = 0; this.lastWhite = 0; this.getToWhite = 0;
    this.voice = 0;                               // 语音频谱驱动，webui 无语音 → 固定 0
  }

  // 每 tick 推进一次。随机数消耗顺序与 sim.py 严格一致 —— 改动会破坏可对齐性。
  Halo.prototype.update = function () {
    var rng = this.rng, i, j, c, r;

    for (i = 0; i < this.connections.length; i++) {
      c = this.connections[i];
      c.lastLightUp = c.lightUp;
      c.lightUp *= 0.9;                            // 约 1s 衰减完
      if (rng() < this.fireChance / TICK_HZ) c.lightUp = 1.0;
    }
    if (rng() < 1 / 60) this.fireChance = Math.pow(rng(), 3);   // 三次方 → 成串触发

    for (i = 0; i < this.rot.length; i++) {        // 抽搐式旋转
      r = this.rot[i];
      r[1] = r[0];
      if (r[0] !== r[3]) {
        r[4] += 1.0 / L(20.0, Math.abs(r[2] - r[3]), 0.5);
        r[0] = L(r[2], r[3], scurve(r[4], 0.5));
        if (r[4] > 1.0) { r[4] = 0.0; r[2] = r[3]; r[0] = r[3]; }
      } else if (rng() < 1 / 30) {
        var sign = rng() < 0.5 ? 1.0 : -1.0;
        r[3] = r[0] + sign * L(15.0, 150.0, rng());
      }
    }

    for (i = 0; i < this.bits.length; i++) {
      for (j = 0; j < this.bits[i].length; j++) this.bits[i][j].update(rng);
    }
    if (rng() < 1 / 60) {                          // 随机整环脉冲
      var target = this.bits[randInt(rng, 0, this.bits.length)];
      for (j = 0; j < target.length; j++) target[j].setToMax();
    }

    this.lastExpand = this.expand;
    this.lastPush = this.push;
    this.lastWhite = this.white;
    this.expand = lat(this.expand, this.getToExpand, 0.05, 0.0125);
    this.push = lat(this.push, this.getToPush, 0.02, 0.025);
    this.white = lat(this.white, this.getToWhite, 0.07, 1 / 44);

    // 以下三个块各消耗一次随机数；两个独立条件互不短路（左操作数总是求值）
    var flag = false;
    if (rng() < 1 / 160) {
      if (rng() < 1 / 8) {
        flag = this.getToWhite < 1.0;
        this.getToWhite = 1.0;
      } else {
        this.getToWhite = 0.0;
      }
    }
    if (rng() < 1 / 160 || flag) {
      if (rng() < 0.5 && flag) {
        this.getToExpand = L(0.8, 2.0, Math.pow(rng(), 1.5));
      } else {
        this.getToExpand = 1.0;
      }
    }
    if (rng() < 1 / 160 || flag) {
      if (rng() < 0.5 && flag) {
        // Range(-1,0) 恒为 -1；Range(1,6) 为 1..5 → getToPush ∈ 0..4，均值 2
        this.getToPush = randInt(rng, -1, 0) + randInt(rng, 1, 6);
      } else {
        this.getToPush = 0.0;
      }
    }
  };

  // radius 第 i 环（i 可为 0.5 等半整数，用于 bit 环中心线）
  Halo.prototype.radius = function (i, t) {
    return ((3.0 + i + L(this.lastPush, this.push, t)) - 0.5 * this.voice) *
           L(this.lastExpand, this.expand, t) * 10.0;
  };

  Halo.prototype.rotation = function (i, t) {
    return L(this.rot[i][1], this.rot[i][0], t);
  };

  /* ── Node 侧测试出口（浏览器下 module 未定义，跳过）。
        测试注入 CPython 兼容的 MT19937 作为 rng，即可与 sim.py 逐帧数值对齐。 ── */
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      L: L, lat: lat, scurve: scurve, deg2vec: deg2vec, dirvec: dirvec,
      bezier3: bezier3, makeRng: makeRng, randInt: randInt,
      MemoryBit: MemoryBit, Connection: Connection, Halo: Halo,
      RING_BITS: RING_BITS, TICK_HZ: TICK_HZ
    };
  }
  if (!HAS_DOM) return;

  /* ─────────────────────────── 色板 */

  var COLORS = {
    dark:  { idle: [150, 190, 178], thinking: [235, 165, 100] },
    light: { idle: [ 84, 138, 120], thinking: [198, 132,  84] }
  };

  /* ─────────────────────────── 渲染 */

  // halo 半径尺度：最外 bit 环外缘 ≈ 59 游戏单位，占视口短边的 ~30%
  var UNITS_OUTER = 59.0;

  function render(halo, t, W, H, rgb, zoom) {
    ctx.clearRect(0, 0, W, H);
    /* 光环锚点：偏右上，避开中央的对话区。
       但要保证整个环（含最外 bit 环）都在画面内 —— 半径 0.30·min(W,H) 经常
       大于 0.2·H，直接放在 20% 高度会把环顶削掉。这里用安全边距兜底。 */
    var rpx = UNITS_OUTER * zoom;
    var cx = Math.min(W * 0.8, W - rpx - 10);
    var cy = Math.max(H * 0.2, rpx + 10);
    var rgbStr = rgb[0] + ',' + rgb[1] + ',' + rgb[2];

    function P(x, y) { return [cx + x * zoom, cy + y * zoom]; }

    /* ── 20 条连线（画在环与 bit 之下，会被外环盖住根部） */
    var rOuter = halo.radius(2, t);
    for (var i = 0; i < halo.connections.length; i++) {
      var c = halo.connections[i];
      if (c.lastLightUp <= 0.05 && c.lightUp <= 0.05) continue;
      var light = 2.0 * L(c.lastLightUp, c.lightUp, t);   // 半宽 ∈[0,2] 游戏单位
      if (light <= 1e-4) continue;

      var p0, p1, p2, p3, radial;
      if (c.stuckAt === null) {
        /* radial：halo 外缘 → 视口边界。
           射线与画布矩形求交，坐标一律用 halo 局部坐标（P() 再乘 zoom）。 */
        radial = true;
        var dx = Math.cos(c.angle), dy = Math.sin(c.angle);
        var x0 = -cx / zoom, x1 = (W - cx) / zoom;
        var y0 = -cy / zoom, y1 = (H - cy) / zoom;
        var tx = dx > 1e-9 ? x1 / dx : (dx < -1e-9 ? x0 / dx : Infinity);
        var ty = dy > 1e-9 ? y1 / dy : (dy < -1e-9 ? y0 / dy : Infinity);
        var tEdge = Math.min(tx, ty) * 1.02;            // 略微出画 → 视觉上"伸出去"
        var span = Math.max(tEdge - rOuter, 1e-3);
        var nx = -dy, ny = dx;                          // 法向，用来做弯曲
        var amp = c.bend * Math.min(span * 0.16, 70);
        p0 = [dx * rOuter, dy * rOuter];                // 起点：紧贴最外环
        p1 = [p0[0] + dx * span * 0.34 + nx * amp,
              p0[1] + dy * span * 0.34 + ny * amp];
        p2 = [p0[0] + dx * span * 0.70 + nx * amp * 0.55,
              p0[1] + dy * span * 0.70 + ny * amp * 0.55];
        p3 = [dx * tEdge, dy * tEdge];                  // 终点：视口边界
      } else {
        /* game：忠实 sim.py。终点 = halo 外缘，锚点可能在画外。 */
        radial = false;
        var dv = dirvec(0, 0, c.stuckAt[0], c.stuckAt[1]);
        p0 = c.stuckAt;
        p1 = c.handle;
        p2 = [dv[0] * 400.0, dv[1] * 400.0];            // 牵引点
        p3 = [dv[0] * rOuter, dv[1] * rOuter];          // 终点 = halo 外缘
      }

      var prev = p0;
      var baseA = 1.0;
      for (var j = 0; j <= 19; j++) {
        var q = bezier3(p0, p1, p2, p3, j / 19.0);
        var segx = q[0] - prev[0], segy = q[1] - prev[1];
        var seg = Math.sqrt(segx * segx + segy * segy);
        if (seg < 1e-6) { prev = q; continue; }
        var dx2 = segx / seg, dy2 = segy / seg;
        var px = -dy2, py = dx2;
        var th = seg * 0.3;                              // 沿曲线的长度，不是粗细
        var A = P(q[0] - dx2 * th - px * light, q[1] - dy2 * th - py * light);
        var B = P(q[0] - dx2 * th + px * light, q[1] - dy2 * th + py * light);
        var C = P(q[0] - px * light, q[1] - py * light);
        var D = P(q[0] + px * light, q[1] + py * light);
        // radial 模式下沿曲线末端渐隐，让神经"溶进"屏幕边缘
        var alpha = radial
          ? baseA * (1.0 - 0.55 * Math.pow(j / 19.0, 1.3))
          : baseA;
        ctx.fillStyle = 'rgba(' + rgbStr + ',' + alpha.toFixed(3) + ')';
        ctx.beginPath();
        ctx.moveTo(A[0], A[1]); ctx.lineTo(B[0], B[1]);
        ctx.lineTo(C[0], C[1]); ctx.lineTo(D[0], D[1]);
        ctx.closePath();
        ctx.fill();
        prev = q;
      }
    }

    /* ── 2 个主环（VectorCircle：alpha 被当作内孔半径比例）
         ring 0 常态 3 单位细环；white→1 时内孔归零 → 实心圆盘 */
    for (var ring = 0; ring < 2; ring++) {
      var rad = halo.radius(ring, t);
      var R = rad * zoom;
      if (R < 1) continue;
      var a;
      if (ring === 0) {
        a = L(3.0 / Math.max(rad, 1e-3), 1.0, L(halo.lastWhite, halo.white, t));
      } else {
        a = 3.0 / Math.max(rad, 1e-3);
      }
      a = Math.min(Math.max(a, 0), 1);
      var rin = (1 - a) * R;
      ctx.beginPath();
      if (rin <= 0.5) {
        ctx.fillStyle = 'rgba(' + rgbStr + ',0.95)';
        ctx.arc(cx, cy, R, 0, Math.PI * 2);
        ctx.fill();
      } else {
        ctx.strokeStyle = 'rgba(' + rgbStr + ',0.9)';
        ctx.lineWidth = R - rin;
        ctx.arc(cx, cy, (R + rin) / 2, 0, Math.PI * 2);
        ctx.stroke();
      }
    }

    /* ── 100 个 bit。精灵 rotation = 该点极角，局部 +Y 落在切向：
          scaleX(grow) 沿切向、scaleY(4) 沿径向 —— 与 sim.py 的
          bit_axis="tangential" 默认值一致（长边沿环生长） */
    ctx.fillStyle = 'rgba(' + rgbStr + ',0.88)';
    for (var ri = 0; ri < halo.bits.length; ri++) {
      var bits = halo.bits[ri];
      var n = bits.length;
      var rot = halo.rotation(ri, t);
      var rr = halo.radius(ri + 0.5, t);
      for (var k = 0; k < n; k++) {
        var ang = k * 360.0 / n + rot;
        var v = deg2vec(ang);
        var grow = 8.0 * bits[k].fill(t);
        if (grow <= 0.3) continue;
        var bx = cx + v[0] * rr * zoom, by = cy + v[1] * rr * zoom;
        var ca = Math.cos(ang * Math.PI / 180), sa = Math.sin(ang * Math.PI / 180);
        var hw = grow * zoom / 2.0, hh = 4.0 * zoom / 2.0;
        ctx.beginPath();
        // 局部顶点 (-hw,-hh),(hw,-hh),(hw,hh),(-hw,hh) 经精灵旋转
        var sx1 = bx + hw * sa - hh * ca, sy1 = by - hw * ca - hh * sa;
        ctx.moveTo(sx1, sy1);
        ctx.lineTo(bx - hw * sa - hh * ca, by + hw * ca - hh * sa);
        ctx.lineTo(bx - hw * sa + hh * ca, by + hw * ca + hh * sa);
        ctx.lineTo(bx + hw * sa + hh * ca, by - hw * ca + hh * sa);
        ctx.closePath();
        ctx.fill();
      }
    }

    /* ── 光照快照：把本帧的光源位置 / 半径 / 强度 / 色写到 DOM，
          供 UI 层做「受光」（halo.js 自己不消费）。
          每帧复用同一对象，不产生 GC 垃圾。 */
    var light = window.__haloLight ||
                (window.__haloLight = { x: 0, y: 0, r: 0, rgb: [0, 0, 0],
                                        intensity: 0, expand: 1, white: 0 });
    var connSum = 0;
    for (var li = 0; li < halo.connections.length; li++) {
      connSum += L(halo.connections[li].lastLightUp, halo.connections[li].lightUp, t);
    }
    light.x = cx;
    light.y = cy;
    light.r = rOuter * zoom;
    light.rgb[0] = rgb[0]; light.rgb[1] = rgb[1]; light.rgb[2] = rgb[2];
    light.expand = L(halo.lastExpand, halo.expand, t);
    light.white  = L(halo.lastWhite,  halo.white,  t);
    // 强度 = 基础 + 呼吸(expand) + 白化脉冲(white) + 连线闪光(connSum，归一到 ~3 即饱和)
    light.intensity = 0.50 + 0.30 * light.expand + 0.90 * light.white +
                      0.25 * Math.min(connSum / 3, 1);
  }

  /* ─────────────────────────── 运行时 */

  var haloEnabled = false;
  var running = false;
  var rafId = 0;
  var lastNow = 0;
  var acc = 0;
  var zoom = 4;
  var W = 0, H = 0;
  var dpr = 1;
  var seed = 2139;
  var haloState = 'idle';

  function makeHalo() {
    var rng = makeRng(seed);
    var h = new Halo(rng);
    for (var i = 0; i < WARMUP_TICKS; i++) h.update();
    return h;
  }
  var halo = makeHalo();

  function currentRgb() {
    var light = document.documentElement.classList.contains('light-theme');
    var set = light ? COLORS.light : COLORS.dark;
    return set[haloState] || set.idle;
  }

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = canvas.clientWidth || window.innerWidth;
    H = canvas.clientHeight || window.innerHeight;
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    // 让最外 bit 环外缘约占短边的 30%
    zoom = (Math.min(W, H) * 0.30) / UNITS_OUTER;
  }

  function frame(now) {
    if (!running) return;
    var dt = now - lastNow;
    lastNow = now;
    if (dt < 0) dt = 0;
    if (dt > MAX_CATCHUP_MS) dt = MAX_CATCHUP_MS;
    acc += dt;
    while (acc >= TICK_MS) {
      halo.update();
      acc -= TICK_MS;
    }
    render(halo, acc / TICK_MS, W, H, currentRgb(), zoom);
    rafId = requestAnimationFrame(frame);
  }

  function start() {
    if (running) return;
    running = true;
    resize();
    lastNow = performance.now();
    acc = 0;
    rafId = requestAnimationFrame(frame);
  }

  function stop() {
    running = false;
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    ctx.clearRect(0, 0, W, H);
  }

  function setHaloEnabled(on) {
    on = !!on;
    haloEnabled = on;
    haloEl.style.display = on ? '' : 'none';
    if (on) {
      start();
    } else {
      stop();
    }
    try { localStorage.setItem('webui_halo', on ? 'on' : 'off'); } catch (e) {}
    return haloEnabled;
  }

  // 空闲/思考 双色切换。思考态只换色，不干预状态机 —— 激发仍由随机事件驱动。
  function setHaloState(state) {
    var next = (state === 'thinking' || state === 'processing') ? 'thinking' : 'idle';
    if (next === haloState) return;
    haloState = next;
    haloEl.setAttribute('data-state', haloState);
  }

  window.setHaloEnabled = setHaloEnabled;
  window.isHaloEnabled = function () { return haloEnabled; };
  window.setHaloState = setHaloState;

  /* ── 外部状态同步：拦截 ui.js 的三个状态函数 + 主题变更 */
  function syncFromSbData() {
    if (typeof _sbData !== 'undefined' && _sbData && _sbData.state) {
      setHaloState(_sbData.state);
    }
  }

  function wrap(name, after) {
    var orig = window[name];
    if (typeof orig !== 'function') return;
    window[name] = function () {
      orig.apply(window, arguments);
      if (after) after.apply(null, arguments);
      syncFromSbData();
    };
  }
  wrap('setProcessing');
  wrap('showThinking');
  wrap('removeThinking', function () { setTimeout(syncFromSbData, 0); });

  new MutationObserver(function () {
    if (running) render(halo, 0, W, H, currentRgb(), zoom);
  }).observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });

  window.addEventListener('resize', function () {
    if (!running) return;
    resize();
    render(halo, 0, W, H, currentRgb(), zoom);
  });

  // 页面不可见时暂停 tick，回来时重跑预热（避免长时间空转后状态跳变）
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      if (running) { running = false; if (rafId) { cancelAnimationFrame(rafId); rafId = 0; } }
    } else if (haloEnabled && !running) {
      lastNow = performance.now();
      acc = 0;
      running = true;
      rafId = requestAnimationFrame(frame);
    }
  });

  /* ── 初始化：默认开启
     光环现在是 UI 的主要视觉（投影投在顶栏/输入区/消息上），
     关掉等于整个界面失去光源。想关仍可从顶栏 💤 按钮或以 'off' 落盘。 */
  var saved = true;
  try { saved = localStorage.getItem('webui_halo') !== 'off'; } catch (e) { saved = true; }
  syncFromSbData();
  if (saved) setHaloEnabled(true);
  else haloEl.style.display = 'none';
})();
