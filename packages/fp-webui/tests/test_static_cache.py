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


def test_index_stamp_is_real_mtime() -> None:
    """index.html 里每个 ?_t= 都等于该文件真实 mtime（不是原样的占位 1）。"""
    html = _client().get("/").text
    stamps = dict(_TS_RE.findall(html))
    assert stamps, "index.html 里没有带 ?_t= 的 css/js 引用"
    for rel, ts in stamps.items():
        assert ts == _mtime(rel), f"{rel} 的缓存戳 {ts} != 真实 mtime {_mtime(rel)}"


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
