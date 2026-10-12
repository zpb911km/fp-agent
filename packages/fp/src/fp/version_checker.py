"""fp/version_checker.py - 版本检查与自动更新

体验优先的两条硬约束：

1. 网络不进关键路径 —— 启动检查在后台守护线程**并行**完成并写入本地缓存
   （数据目录 version_cache.json，24h 内不重复请求 PyPI）；前端只在安全点
   （L1 欢迎区 / TUI 欢迎屏）读缓存渲染提示，读取零网络等待。
   绝不从后台线程直接 print：那会砸穿 TUI 屏幕 / 打断用户正在输入的行，
   还会让提示在任意时刻（可能已聊到一半）突兀冒出。
2. 只升级「本地已安装」的组件 —— 缺失的包绝不强装（旧版
   `pip install fp-agent[all]` 整批安装 5 包、一包失败全体回退，已废弃）；
   逐包执行 pip，单包失败只影响它自己，其余照常升级，并逐包报告结果。
"""

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

try:  # 版本语义比较优先走 PEP440（rc/a/b 后缀正确可比），缺失则退回纯数字段
    from packaging.version import Version as _PepVersion
except ImportError:  # pragma: no cover
    _PepVersion = None

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"

# 需要检查的包：(PyPI 包名, 显示名称)
PACKAGES = [
    ("fp-agent", "fp"),
    ("fp-core", "fp-core"),
    ("fp-terminal", "fp-terminal"),
    ("fp-webui", "fp-webui"),
    ("fp-acp", "fp-acp"),
]

FETCH_TIMEOUT = 3.0  # 单包查询超时（秒）——并行执行，总耗时≈最慢一包
CACHE_TTL = 24 * 3600  # 缓存有效期：一天内不重复打扰 PyPI


# ── 工具函数 ───────────────────────────────────────


def _parse_version(ver: str) -> tuple[int, ...]:
    """把 '0.1.5' 逐段取数字前缀转成 (0, 1, 5) 以便比较。

    '0.4.0rc1' → (0, 4, 0)：后缀段无法整体转 int 时取其数字前缀后停止。
    """
    parts: list[int] = []
    for seg in str(ver).split("."):
        digits = ""
        for ch in seg:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or (0,)


def _newer(latest: str, installed: str) -> bool:
    """latest 是否比 installed 新（优先 PEP440 语义，失败退回数字段比较）"""
    if _PepVersion is not None:
        with contextlib.suppress(Exception):
            return _PepVersion(latest) > _PepVersion(installed)
    return _parse_version(latest) > _parse_version(installed)


def _get_installed_version(pkg_name: str) -> str | None:
    """用 importlib.metadata 获取已安装版本；未安装返回 None"""
    try:
        from importlib.metadata import version

        return version(pkg_name)
    except Exception:
        return None


def _fetch_latest(pkg_name: str) -> str | None:
    """查询 PyPI JSON API 获取最新版本号（单包，带短超时）"""
    url = f"https://pypi.org/pypi/{pkg_name}/json"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "fp-agent/version-checker/2.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            data = json.loads(resp.read())
            return data["info"]["version"]
    except Exception:
        return None


def _fetch_latest_map(names: list[str]) -> dict[str, str]:
    """并行查询多个包的最新版本：{pypi名: 版本}，查询失败的包不入表。

    并行 + 短超时，N 个包的总耗时 ≈ 单包最慢值而非 N 倍超时。
    """
    if not names:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(names))) as pool:
        results = pool.map(_fetch_latest, names)
        return {name: ver for name, ver in zip(names, results, strict=True) if ver}


# ── 本地缓存 ───────────────────────────────────────


def _cache_path() -> str:
    from fp_core.platform_utils import get_data_dir

    return os.path.join(get_data_dir(), "version_cache.json")


def _load_cache() -> dict[str, Any]:
    """读缓存：{checked_at: float, latest: {pypi名: 版本}}，任何异常返回 {}"""
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            data = json.load(f)
        obj = cast(dict[str, Any], data)
        if isinstance(data, dict) and isinstance(obj.get("latest"), dict):
            return obj
    except Exception:
        pass
    return {}


def _cached_latest(cache: dict[str, Any]) -> dict[str, str]:
    """从缓存 dict 中取出并规整 latest 映射（键值强制 str，脏数据丢弃）"""
    raw = cache.get("latest")
    if isinstance(raw, dict):
        items = cast(dict[str, Any], raw)
        return {str(k): str(v) for k, v in items.items()}
    return {}


def _save_cache(latest: dict[str, str], checked_at: float | None = None):
    """原子写缓存（tmp + replace），失败静默——缓存只是体验优化，不是数据"""
    path = _cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"checked_at": checked_at or time.time(), "latest": latest}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _refresh_cache() -> bool:
    """刷新缓存：TTL 未到或无已装组件时跳过网络；失败静默返回 False。"""
    with contextlib.suppress(Exception):
        if os.environ.get("FP_NO_UPDATE_CHECK"):
            return False
        cache = _load_cache()
        if time.time() - float(cache.get("checked_at", 0)) < CACHE_TTL:
            return False
        names = [p for p, _ in PACKAGES if _get_installed_version(p)]
        if not names:
            return False
        merged = _cached_latest(cache)
        fetched = _fetch_latest_map(names)
        if not fetched:
            return False
        merged.update(fetched)
        _save_cache(merged)
        return True
    return False


# ── 安全点提示（前端入口） ──────────────────────────


def _outdated_rows(latest_map: dict[str, str]) -> list[tuple[str, str, str, str]]:
    """比对本地已装版本，返回 [(pypi名, 显示名, 本地版, 最新版), ...]"""
    rows: list[tuple[str, str, str, str]] = []
    for pypi, display in PACKAGES:
        installed = _get_installed_version(pypi)
        if installed is None:
            continue
        latest = latest_map.get(pypi)
        if latest and _newer(latest, installed):
            rows.append((pypi, display, installed, latest))
    return rows


def take_notice() -> str | None:
    """更新提示（供前端在**安全点**渲染）：只读本地缓存，零网络、无 ANSI。

    无缓存（首装本次启动刚触发后台检查）或无更新时返回 None——
    提示宁可晚一个启动周期，也绝不阻塞、绝不砸屏。
    """
    rows = _outdated_rows(_cached_latest(_load_cache()))
    if not rows:
        return None
    items = [f"{disp} {old} → {new}" for _, disp, old, new in rows]
    shown = "、".join(items[:3]) + (f" 等 {len(items)} 项" if len(items) > 3 else "")
    return f"💡 fp 有新版本：{shown}（运行 fp --update 升级）"


def check_updates_background():
    """后台守护线程刷新版本缓存：并行查询、TTL 内不重复、失败静默。

    本函数自身不做任何 I/O 打印——提示由前端在安全点经 take_notice() 取用。
    """
    thread = threading.Thread(target=_refresh_cache, name="fp-version-check", daemon=True)
    thread.start()


# ── 更新执行（`fp --update` 入口） ──────────────────


def _pip_upgrade(pypi_name: str) -> tuple[bool, str]:
    """单包升级：独立 pip 进程 + 捕获输出（屏幕保持干净）。返回 (成功?, 失败详情)"""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check", pypi_name],
            capture_output=True,
            text=True,
        )
    except Exception as e:  # pragma: no cover - pip 进程都起不来属极端环境
        return False, str(e)
    if proc.returncode == 0:
        return True, ""
    return False, (proc.stderr or "") + (proc.stdout or "")


def do_update() -> int:
    """检查并升级「本地已安装」的 fp 组件（同步）。返回进程退出码 0/1。

    - 只升级已安装的包：缺的组件仅提示、绝不强装（旧行为：fp-agent[all]
      强制补齐 5 包，任一包失败 → pip 整批回退，一个版本都没升上去）。
    - 逐包 pip：单包失败只影响它自己，其余照常升级，最后逐包汇报。
    """
    installed = [(p, d) for p, d in PACKAGES if _get_installed_version(p)]
    if not installed:
        print(f"{YELLOW}!{RESET} 未检测到 pip 安装的 fp 组件（源码运行？），无法自动升级")
        print("  手动升级：仓库根执行 git pull && pip install -e .")
        return 1

    installed_names = {p for p, _ in installed}
    missing = [d for p, d in PACKAGES if p not in installed_names]
    names = [p for p, _ in installed]

    print(f"正在查询 PyPI（{len(names)} 个组件并行，超时 {FETCH_TIMEOUT:.0f}s）…")
    latest_map = _fetch_latest_map(names)
    if latest_map:  # 顺手刷新本地缓存，下次启动的安全点提示即为最新
        merged = _cached_latest(_load_cache())
        merged.update(latest_map)
        _save_cache(merged)

    failed_query = [d for p, d in installed if p not in latest_map]
    rows = _outdated_rows(latest_map)

    if failed_query:
        print(f"{YELLOW}!{RESET} 查询失败（网络不可达？），本次跳过：{'、'.join(failed_query)}")
    if missing:
        print(f"  未安装的组件保持原样：{'、'.join(missing)}（需要时再 pip install <包名>）")

    if not rows:
        if failed_query:
            print(f"{YELLOW}!{RESET} 未能确认全部组件状态，已确认部分未发现新版本")
            return 1
        print(f"{GREEN}✓{RESET} 已安装组件均为最新")
        return 0

    ok: list[str] = []
    bad: list[str] = []  # 显示名（汇报用）
    bad_pypi: list[str] = []  # PyPI 包名（手动重试命令用，fp ≠ fp-agent）
    for pypi, disp, old, new in rows:
        print(f"  {disp:<12} {old} → {GREEN}{new}{RESET}  正在升级…", end="", flush=True)
        success, detail = _pip_upgrade(pypi)
        if success:
            ok.append(disp)
            print(f" {GREEN}✓ 完成{RESET}")
        else:
            bad.append(disp)
            bad_pypi.append(pypi)
            print(f" {RED}✗ 失败{RESET}")
            for line in detail.strip().splitlines()[-4:]:
                print(f"      {line}")

    if bad:
        print(f"\n{YELLOW}!{RESET} 成功 {len(ok)} 项、失败 {len(bad)} 项：{'、'.join(bad)}")
        print(f"  可手动重试：pip install -U {' '.join(bad_pypi)}")
        return 1
    print(f"\n{GREEN}✓{RESET} 更新完成（{len(ok)} 项）")
    return 0
