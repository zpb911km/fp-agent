"""e2e 夹具：起 WebUI 服务器子进程 → 等 /api/health 就绪 → 提供 token/base_url → 收尾杀进程。

与单测分离：tests/test_*.py 不起服务器；e2e（-m e2e）需要真实 HTTP + WS + Chromium。

端口策略（每一步都要有据可查，不再「静默复用」）：
    1. 8899（可用 ``FP_E2E_PORT`` 改）空闲 → 自己起一个，token 从自己那份数据目录读；
    2. 该端口已有健康服务，且它认某个候选 token（真调一次 ``/api/auth`` 验通）→ 复用它，
       但**显式告警**：那进程跑的是它自己那份代码，不保证等于当前工作树；
    3. 该端口健康但**所有**候选 token 都验不通（别人的 / 残留的开发服务）→ 不猜也不
       静默跳过，改用临时空闲端口起自己的服务：既不假绿，也不丢覆盖率。

为什么需要「候选 token」这套：token 落在**服务器自己**的数据目录里。我们起的子进程继承
本进程 env，路径两边必然一致；但人工在干净 shell 里起的开发服务用的是真实用户目录，
而本进程的目录已被根 ``conftest.py`` 钉到临时目录（见那个文件的「用户目录隔离」）。所以
候选取两处：当前（钉住的）数据目录 + 摘掉钉住后复现的真实用户目录，逐個验通即用。
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import warnings
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import expect

REPO_ROOT = Path(__file__).resolve().parents[4]  # .../packages/fp-webui/tests/e2e → 仓库根
SRCS = [REPO_ROOT / "packages" / "fp-webui" / "src", REPO_ROOT / "packages" / "fp-core" / "src"]
for _p in SRCS:  # 忘带 PYTHONPATH 也能跑（与 pyproject pythonpath 双保险）
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

PORT = int(os.environ.get("FP_E2E_PORT", "8899"))  # 刻意避开默认 8765；可用 FP_E2E_PORT 覆盖
_DIR_VARS = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA")


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: 端到端（需起 WebUI 服务器 + Chromium）")


def _base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _healthy(base: str) -> bool:
    try:
        with urllib.request.urlopen(base + "/api/health", timeout=2) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:  # 4xx/5xx 也是「有响应」，但句柄得关掉，否则 ResourceWarning
        e.close()
        return False
    except Exception:
        return False


def _free_port() -> int:
    """向内核要一个当前空闲的端口（绑定 0 后立刻释放，竞态窗口极小）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.contextmanager
def _unpinned_env() -> Iterator[None]:
    """临时摘掉根 conftest 钉住的目录变量 → 复现「干净 shell」下的真实用户目录。"""
    saved = {k: os.environ.pop(k, None) for k in _DIR_VARS}
    try:
        yield
    finally:
        for key, val in saved.items():
            if val is not None:
                os.environ[key] = val


def _token_candidates() -> list[str]:
    """可能的 token 值：显式 env 覆盖 > 当前数据目录 > 真实用户目录。去重保序。"""
    from fp_core.platform_utils import get_data_dir  # 与 main.py 同源取 token 路径

    files = [Path(get_data_dir()) / "tokens" / ".webui_token"]
    with _unpinned_env():  # 外部服务由人在干净 shell 里起的 → token 在真实目录
        files.append(Path(get_data_dir()) / "tokens" / ".webui_token")

    out: list[str] = []
    env_token = os.environ.get("FP_WEBUI_TOKEN", "").strip()
    if env_token:
        out.append(env_token)
    for path in files:
        with contextlib.suppress(OSError):
            token = path.read_text().strip()
            if token and token not in out:
                out.append(token)
    return out


def _token_ok(base: str, token: str) -> bool:
    """问服务器本人「这 token 你认吗」——认才是同一个实例，不认就别拿它跑断言。"""
    req = urllib.request.Request(
        base + "/api/auth",
        data=json.dumps({"token": token}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:  # 401 是**预期**答案（token 不归它），close 掉免 ResourceWarning
        e.close()
        return False
    except Exception:
        return False


def _tail(log_path: str, n: int = 2000) -> str:
    with contextlib.suppress(OSError):
        return Path(log_path).read_text(errors="replace")[-n:]
    return "(无日志)"


def _kill(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError):  # 杀整个进程组（含 portal 子进程）
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def _spawn(port: int, base: str) -> tuple[subprocess.Popen[bytes], str]:
    """起一个自己的 WebUI 子进程并等就绪；起不来抛 RuntimeError（带日志尾巴）。"""
    env = dict(os.environ, PYTHONPATH=":".join(str(p) for p in SRCS))
    with tempfile.NamedTemporaryFile(delete=False, suffix=".log", prefix="fp_e2e_server_") as log:
        log_path = log.name
    # 注意：stdout 传路径而非文件句柄——with 退出即关闭句柄，传句柄会
    # 触发 "ValueError: I/O operation on closed file"（曾致 12 个 e2e 全 ERROR）。
    with open(log_path, "ab") as log_fp:
        proc = subprocess.Popen(
            [sys.executable, "-m", "fp_webui.main", "--port", str(port)],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.time() + 90
    while time.time() < deadline:
        if proc.poll() is not None:
            _kill(proc)
            raise RuntimeError(f"fp_webui 启动即退出(code={proc.returncode}):\n{_tail(log_path)}")
        if _healthy(base):
            return proc, log_path
        time.sleep(0.5)
    _kill(proc)
    raise RuntimeError(f"fp_webui 90s 内未就绪({base}):\n{_tail(log_path)}")


def _read_own_token() -> str:
    from fp_core.platform_utils import get_data_dir  # 与 main.py 同源取 token 路径

    token_file = Path(get_data_dir()) / "tokens" / ".webui_token"
    token = token_file.read_text().strip()
    assert len(token) >= 32, f"token 文件异常: {token_file}"
    return token


@pytest.fixture(scope="session")
def server():
    """session 级服务器：子进程启动 + 就绪探针 + 退出时杀整个进程组。"""
    port = PORT
    base = _base_url(port)
    external_token: str | None = None

    if _healthy(base):
        external_token = next((t for t in _token_candidates() if _token_ok(base, t)), None)
        if external_token is None:
            port = _free_port()
            base = _base_url(port)
            warnings.warn(
                f"{_base_url(PORT)} 上有一个健康但**不认我们任何候选 token** 的 WebUI 服务"
                f"（别人的 / 残留的开发服务），无法确认它跑的是哪份代码。已改用空闲端口 {port} "
                f"起独立服务跑 e2e；要在 {PORT} 上跑请先停掉它，或用 FP_E2E_PORT 指定端口。",
                stacklevel=1,
            )
        else:
            warnings.warn(
                f"复用 {base} 上**外部启动**的 WebUI 服务（token 经 /api/auth 验通）：它跑的是它"
                f"自己那份代码，不保证等于当前工作树——要校验新代码请先停掉它，或用 FP_E2E_PORT 换端口。",
                stacklevel=1,
            )

    proc: subprocess.Popen[bytes] | None = None
    if external_token is None:
        try:
            proc, _log_path = _spawn(port, base)
        except RuntimeError as first:
            free = _free_port()
            try:
                proc, _log_path = _spawn(free, _base_url(free))
            except RuntimeError as second:
                raise RuntimeError(
                    f"端口 {port} 起不来（可能被非 FP 服务占用）：\n{first}\n\n改用空闲端口 {free} 也失败：\n{second}"
                ) from None
            base = _base_url(free)
        token = _read_own_token()
    else:
        token = external_token

    yield SimpleNamespace(base_url=base, token=token, external=proc is None)

    if proc is not None:
        _kill(proc)


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
