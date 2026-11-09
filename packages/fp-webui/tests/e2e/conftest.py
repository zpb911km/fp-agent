"""e2e 夹具：起 WebUI 服务器子进程 → 等 /api/health 就绪 → 提供 token/base_url → 收尾杀进程。

与单测分离：tests/test_*.py 不起服务器；e2e（-m e2e）需要真实 HTTP + WS + Chromium。
若 8899 已有健康服务（残留/人工启动）则复用而非抢占，避免端口冲突假绿。
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import expect

REPO_ROOT = Path(__file__).resolve().parents[4]  # .../packages/fp-webui/tests/e2e → 仓库根
SRCS = [REPO_ROOT / "packages" / "fp-webui" / "src", REPO_ROOT / "packages" / "fp-core" / "src"]
for _p in SRCS:  # 忘带 PYTHONPATH 也能跑（与 pyproject pythonpath 双保险）
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

PORT = 8899  # 刻意避开默认 8765
BASE_URL = f"http://127.0.0.1:{PORT}"


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: 端到端（需起 WebUI 服务器 + Chromium）")


def _healthy() -> bool:
    try:
        with urllib.request.urlopen(BASE_URL + "/api/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


@pytest.fixture(scope="session")
def server():
    """session 级服务器：子进程启动 + 就绪探针 + 退出时杀整个进程组。"""
    proc = None
    log_path = None
    if not _healthy():  # 端口已被占用但不健康 → 下面启动会失败并给出日志
        env = dict(os.environ, PYTHONPATH=":".join(str(p) for p in SRCS))
        with tempfile.NamedTemporaryFile(delete=False, suffix=".log", prefix="fp_e2e_server_") as log:
            log_path = log.name
        # 注意：stdout 传路径而非文件句柄——with 退出即关闭句柄，传句柄会
        # 触发 "ValueError: I/O operation on closed file"（曾致 12 个 e2e 全 ERROR）。
        with open(log_path, "ab") as log_fp:
            proc = subprocess.Popen(
                [sys.executable, "-m", "fp_webui.main", "--port", str(PORT)],
                cwd=str(REPO_ROOT),
                env=env,
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.time() + 90
        while time.time() < deadline:
            if proc.poll() is not None:
                tail = Path(log_path).read_text(errors="replace")[-2000:]
                raise RuntimeError(f"fp_webui 启动即退出(code={proc.returncode}):\n{tail}")
            if _healthy():
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("fp_webui 90s 内未就绪: " + Path(log_path).read_text(errors="replace")[-2000:])

    from fp_core.platform_utils import get_data_dir  # 与 main.py 同源取 token 路径

    token_file = Path(get_data_dir()) / "tokens" / ".webui_token"
    token = token_file.read_text().strip()
    assert len(token) >= 32, f"token 文件异常: {token_file}"

    yield SimpleNamespace(base_url=BASE_URL, token=token, external=proc is None)

    if proc is not None:
        with contextlib.suppress(ProcessLookupError):  # 杀整个进程组（含 portal 子进程）
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


@pytest.fixture()
def browser():
    # function 级：session 级 sync_playwright 的 greenlet/asyncio loop 会活到
    # session 结束，污染后续非 e2e 单测的 asyncio.run()（running loop 报错）。
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture()
def errlog():
    """按用例隔离的错误账本：console error / pageerror / 请求失败。"""
    return {"console": [], "pageerror": [], "failed": []}


@pytest.fixture()
def page(server, browser, errlog):
    """已登录的页面：goto → 填 token → 点登录 → 等遮罩消失，全程记录错误。"""
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.set_default_timeout(15000)
    pg.on(
        "console",
        lambda m: errlog["console"].append((m.text, (m.location or {}).get("url", ""))) if m.type == "error" else None,
    )
    pg.on("pageerror", lambda e: errlog["pageerror"].append(str(e)))
    pg.on("requestfailed", lambda r: errlog["failed"].append(r.url))
    pg.goto(server.base_url + "/", wait_until="domcontentloaded")
    pg.fill("#authInput", server.token)
    pg.click("#authBtn")
    try:
        expect(pg.locator("#authOverlay")).to_be_hidden(timeout=20000)  # 登录成功=遮罩消失
    except AssertionError:
        raise AssertionError(
            f"登录失败: authError={pg.locator('#authError').inner_text()!r} "
            f"status={pg.locator('#connectionStatus').inner_text()!r} "
            f"console={errlog['console'][-5:]} pageerr={errlog['pageerror'][-3:]}"
        ) from None
    yield pg
    ctx.close()
