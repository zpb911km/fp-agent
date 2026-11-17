"""静态资源交付契约：改了前端必须能被浏览器拿到。

由来（实测事故）：`features/sessions.js` 把 kw 显示数从写死 3 改为读后端
`kw_show`，磁盘、`/static` 响应、接口全对，用户刷新后界面**仍是 3 个**。
根因两条，都在"交付"这一层：

1. `_stamp_cache` 把 `?_t=` 的解析写成 `group(1).split("/")[-1]`，即只取文件名
   去 `static/` 根目录找；而 css/js 都在子目录 → `OSError` 静默回退原值。
   这套缓存戳自 CSS/JS 分目录起就从未生效（index.html 里恒为 `?_t=1`）。
2. 前端是 ESM import 图，`import ... from './features/sessions.js'` 的 URL 写死
   在模块里，无法逐个打戳；StaticFiles 又不发 Cache-Control，浏览器按启发式
   新鲜期缓存（≈(now - Last-Modified) 的 10%），文件越旧缓存越久。

故此处钉死两条：index.html 的戳必须是**真实 mtime**，`/static/*` 必须
`no-cache`（配 ETag 走 304，代价近零）。
"""

from __future__ import annotations

import os
import re

from fastapi.testclient import TestClient

from fp_webui import main

_TS_RE = re.compile(r"/static/([A-Za-z0-9_./-]+\.(?:css|js))\?_t=(\d+)")


def _client() -> TestClient:
    return TestClient(main.app)


def _mtime(rel: str) -> str:
    return str(int(os.path.getmtime(os.path.join(main._static_dir, rel))))


def _tree_stamp() -> str:
    """整棵 static 树内前端资产（js/css/html）的最大 mtime —— 测试侧独立算。"""
    newest = 0.0
    for root, _dirs, files in os.walk(main._static_dir):
        for fn in files:
            if fn.endswith((".js", ".css", ".html")):
                newest = max(newest, os.path.getmtime(os.path.join(root, fn)))
    return str(int(newest))


def test_index_stamp_css_is_own_mtime() -> None:
    """CSS 的戳是本文件 mtime：改哪个失效哪个，细粒度缓存不被牵连。"""
    html = _client().get("/").text
    stamps = dict(_TS_RE.findall(html))
    css = {rel: ts for rel, ts in stamps.items() if rel.endswith(".css")}
    assert css, "index.html 里没有带 ?_t= 的 css 引用"
    for rel, ts in css.items():
        assert ts == _mtime(rel), f"{rel} 的缓存戳 {ts} != 真实 mtime {_mtime(rel)}"


def test_index_stamp_js_is_whole_tree() -> None:
    """JS 入口的戳是**整棵树**的最大 mtime，不是该文件自身的。

    ESM import 图里 index.html 只引 main.js，而改动十有八九落在叶子
    （features/wire.js、components/*.js）。若按文件自身 mtime 打戳，版本串
    对它恒定不变 → core/build.js 的"资产版本自检"（信号②）完全瞎掉，
    「改了前端、页面还是老行为」就又不透明了。
    """
    html = _client().get("/").text
    stamps = dict(_TS_RE.findall(html))
    js = {rel: ts for rel, ts in stamps.items() if rel.endswith(".js")}
    assert js, "index.html 里没有带 ?_t= 的 js 引用"
    tree = _tree_stamp()
    for rel, ts in js.items():
        assert ts == tree, f"{rel} 的缓存戳 {ts} 应为整树戳 {tree}"


def test_index_stamps_esm_entry_modules() -> None:
    """两个 ESM 入口（spinner/main）必须带戳——它们是整个 import 图的根。"""
    html = _client().get("/").text
    stamps = dict(_TS_RE.findall(html))
    for entry in ("js/main.js", "js/core/spinner.js"):
        assert entry in stamps, f"入口模块 {entry} 未打缓存戳"


def test_static_assets_are_no_cache() -> None:
    """/static/* 一律 no-cache，否则深层 ESM import 会被浏览器长期缓存。"""
    c = _client()
    for rel in ("js/features/sessions.js", "js/main.js", "css/base.css"):
        r = c.get(f"/static/{rel}")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-cache", f"{rel} 缺 no-cache"


def test_index_html_itself_not_heuristically_cached() -> None:
    """主文档同样不得被启发式缓存（否则整个 import 图都不会重新走）。"""
    r = _client().get("/")
    assert r.headers.get("cache-control") == "no-cache"
