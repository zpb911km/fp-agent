"""前端构建戳契约（/api/build）：让"我改了代码，你的页面还是老模块"可见。

由来：前端是 ESM import 图，模块 URL 写死在 import 语句里，改文件不改 URL；
`Cache-Control: no-cache` 也只在**重新导航**时生效。agent 热重启、WS 断线重连、
手机端 bfcache 回前台都不会重新下载模块——于是"改完没生效"被反复误读成
"没改好"（同一类困惑发生过多次）。前端启动取一次作基准、每次连上再取一次，
戳变大就在状态栏露一个「点击刷新」。

钉子三条：
① 戳 = static 树内 js/css/html 的最大 mtime（只增不减地跟随最新一次前端改动）；
② 端点必须在**认证白名单**里——它要在登录前就能取到，那正是基准时刻；
③ 只回一个整数，不含会话/模型/paths 等任何信息（公开端点的最小暴露面）。
"""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from fp_webui import main


def _client() -> TestClient:
    return TestClient(main.app)


def test_build_stamp_matches_newest_frontend_asset() -> None:
    """戳 == static 树内前端资产的最大 mtime（改动任一 js/css/html 都会推高它）。"""
    newest = max(
        int(os.path.getmtime(os.path.join(root, fn)))
        for root, _dirs, files in os.walk(main._static_dir)
        for fn in files
        if fn.endswith((".js", ".css", ".html"))
    )
    r = _client().get("/api/build")
    assert r.status_code == 200
    assert r.json() == {"stamp": newest}


def test_build_stamp_reachable_without_token() -> None:
    """认证白名单：无 Authorization 也必须 200（登录前就要立基准）。"""
    r = TestClient(main.app).get("/api/build")  # 不带任何 header
    assert r.status_code == 200, r.text


def test_build_stamp_leaks_nothing_but_the_integer() -> None:
    """公开端点只暴露一个 mtime 整数，不得带出路径/会话/模型。"""
    body = _client().get("/api/build").json()
    assert list(body) == ["stamp"]
    assert isinstance(body["stamp"], int) and body["stamp"] > 0
