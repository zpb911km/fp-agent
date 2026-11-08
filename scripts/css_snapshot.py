#!/usr/bin/env python3
"""CSS 级联快照：把页面计算样式 + 截图落盘，供 @layer 分层前后 diff。

用法：
  python scripts/css_snapshot.py out_before # 改前
  python scripts/css_snapshot.py out_after   # 改后
  python scripts/css_snapshot.py --diff out_before out_after
"""

from __future__ import annotations

import functools
import http.server
import json
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "packages" / "fp-webui" / "src" / "fp_webui"
INDEX_URL_PATH = "/static/index.html"

PROPS = [
    "display",
    "position",
    "color",
    "background-color",
    "font-size",
    "margin",
    "padding",
    "border",
    "z-index",
    "opacity",
    "flex-direction",
    "gap",
    "width",
    "height",
    "overflow",
    "mix-blend-mode",
    "transition",
]

DUMP_JS = r"""
(() => {
  const keys = new Map();
  for (const el of document.querySelectorAll('*')) {
    let k = el.tagName.toLowerCase();
    if (el.id) k += '#' + el.id;
    const c = el.className && typeof el.className === 'string'
      ? el.className.trim().split(/\s+/).filter(Boolean).slice(0, 3).join('.') : '';
    if (c) k += '.' + c;
    if (!keys.has(k)) keys.set(k, 0);
    keys.set(k, keys.get(k) + 1);
  }
  const out = {};
  for (const [k] of keys) {
    const el = document.querySelector(k);
    if (!el) continue;
    const cs = getComputedStyle(el);
    const v = {};
    for (const p of %PROPS%) v[p] = cs.getPropertyValue(p);
    out[k] = v;
  }
  const root = {};
  for (const p of getComputedStyle(document.documentElement))
    if (p.startsWith('--')) root[p] = getComputedStyle(document.documentElement).getPropertyValue(p);
  return { rules: out, rootVars: root };
})()
""".replace("%PROPS%", json.dumps(PROPS))


def serve() -> tuple[str, http.server.HTTPServer]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(STATIC))
    handler.log_message = lambda *a, **k: None
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}{INDEX_URL_PATH}", srv


def snap(out_prefix: str) -> None:
    from playwright.sync_api import sync_playwright

    url, srv = serve()
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            pg = b.new_page(viewport={"width": 1440, "height": 900})
            pg.add_init_script("sessionStorage.setItem('webui_token','snap-token');")
            pg.goto(url, wait_until="networkidle", timeout=30000)
            pg.wait_for_timeout(800)
            data = pg.evaluate(DUMP_JS)
            pg.screenshot(path=f"{out_prefix}.png", full_page=False)
            b.close()
        Path(f"{out_prefix}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True))
        print(f"OK {out_prefix}.json ({len(data['rules'])} selectors) + .png")
    finally:
        srv.shutdown()


def diff(a: str, b: str) -> int:
    da = json.loads(Path(f"{a}.json").read_text())
    db = json.loads(Path(f"{b}.json").read_text())
    n = 0
    for k in sorted(set(da["rules"]) | set(db["rules"])):
        ra, rb = da["rules"].get(k), db["rules"].get(k)
        if ra != rb:
            n += 1
            print(f"[selector] {k}")
            if ra is None or rb is None:
                print(f"   {'missing in B' if ra else 'missing in A'}")
                continue
            for p in PROPS:
                if ra.get(p) != rb.get(p):
                    print(f"   {p}: {ra.get(p)!r} -> {rb.get(p)!r}")
    for p in sorted(set(da["rootVars"]) | set(db["rootVars"])):
        if da["rootVars"].get(p) != db["rootVars"].get(p):
            n += 1
            print(f"[root var] {p}: {da['rootVars'].get(p)!r} -> {db['rootVars'].get(p)!r}")
    print(f"\n共 {n} 处差异")
    pa, pb = Path(f"{a}.png"), Path(f"{b}.png")
    if pa.exists() and pb.exists():
        print(f"截图: {a}.png ({pa.stat().st_size}B) vs {b}.png ({pb.stat().st_size}B)")
    return n


if __name__ == "__main__":
    if sys.argv[1] == "--diff":
        sys.exit(0 if diff(sys.argv[2], sys.argv[3]) == 0 else 1)
    snap(sys.argv[1])
