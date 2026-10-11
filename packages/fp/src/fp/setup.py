"""首跑配置门禁与交互式引导向导。

职责（对应设计任务 #168）：
- **硬门禁**：LLM 三键（api_key / base_url / model）任一缺失 → 交互终端进向导；
  非交互环境（管道 / --headless / -m / acp / subagent 子进程）打印
  面向用户的引导块并以非 0 退出，绝不阻塞、绝不静默 exit 0。
  cli 与 webui 都可进向导（stdin 属于用户）；acp 不可（stdin 是 JSON-RPC 协议）。
- **零默认**：不生成任何带占位值的 config.json 模板；三键必须由用户显式输入。
- **平台一致**：配置路径、隐藏输入、编码、原子写均对 Linux/macOS/Windows 一致。
- **本次生效**：写盘后 importlib.reload(fp_core.config)，当前启动即用新值，
  无需用户重启（config 在所有消费者处都是模块引用，reload 原地更新安全）。

落点：由 fp.main 在顶层 argparse 之后（mode/headless 已知）、副作用
（ext 迁移 / 版本检查）之前调用。
"""

from __future__ import annotations

import contextlib
import getpass
import importlib
import json
import os
import sys
import tempfile
import warnings
from collections.abc import Iterable
from typing import Any

from fp_core.platform_utils import restrict_file_permissions


class _AbortError(Exception):
    """用户主动取消向导（Ctrl+C / EOF / 空输入）。"""


# ═══════════════════════════════════════════════════════════════
# 输出辅助（编码 / ASCII 保底）
# ═══════════════════════════════════════════════════════════════


def _ensure_utf8() -> None:
    """尽力把标准流切到 UTF-8（Windows 控制台常见 GBK，避免中文乱码/报错）。"""
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            # 某些流实现（StringIO 等）没有 reconfigure，任何不支持都应静默降级
            with contextlib.suppress(Exception):
                reconf(encoding="utf-8")


def _sym(unicode_ch: str, ascii_ch: str) -> str:
    """若当前输出流无法编码该 Unicode 符号，退回 ASCII 保底。"""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        unicode_ch.encode(enc)
        return unicode_ch
    except (UnicodeEncodeError, LookupError):
        return ascii_ch


def _say(text: str = "") -> None:
    """带 ASCII 保底的打印（编码失败时替换为 ? 而非崩溃）。"""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))
    sys.stdout.flush()


# ═══════════════════════════════════════════════════════════════
# 交互性判定
# ═══════════════════════════════════════════════════════════════


def _is_interactive(mode: str, passthrough: Iterable[str]) -> bool:
    """是否应在交互终端进入向导。

    仅当：非 acp ∧ 非 subagent 子进程 ∧ 非 headless/单次消息 ∧ stdin 是 tty。

    cli 与 webui 均放行：两者的 stdin 都属于用户（webui 根本不读 stdin，
    浏览器走独立 WebSocket 通道）。acp 排除——其 stdin 是 IDE 的
    JSON-RPC 协议流（server.py 用 connect_read_pipe 逐行读），占用即破坏协议。
    """
    if mode == "acp":
        return False
    if os.environ.get("FP_IS_SUBAGENT") == "1" or os.environ.get("FP_SUBAGENT_ROLE"):
        return False
    for tok in passthrough:
        if tok == "--headless" or tok == "--message" or tok.startswith("--message="):
            return False
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except (ValueError, OSError):
        return False


# ═══════════════════════════════════════════════════════════════
# 原子写盘
# ═══════════════════════════════════════════════════════════════


def _atomic_write_json(path: str, data: dict[str, Any]) -> None:
    """原子写入 JSON：同目录临时文件 + os.replace；POSIX 下 chmod 600。

    - newline="\\n"：避免 Windows 默认 CRLF 污染配置
    - ensure_ascii=False：保留可读的中文/符号
    """
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        restrict_file_permissions(tmp, 0o600)  # Windows 上是 no-op（无 POSIX 权限位）
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ═══════════════════════════════════════════════════════════════
# 输入
# ═══════════════════════════════════════════════════════════════


def _read_secret(label: str) -> str:
    """隐藏输入；终端不支持关回显时显式告知『输入将可见』而非静默明文。"""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(f"  {label}: ")
    except getpass.GetPassWarning:
        _say("  （当前终端无法隐藏输入，以下输入将可见）")
        try:
            return input(f"  {label}: ")
        except (EOFError, KeyboardInterrupt):
            raise _AbortError from None
    except (EOFError, KeyboardInterrupt):
        raise _AbortError from None


def _read_text(label: str) -> str:
    try:
        return input(f"  {label}: ")
    except (EOFError, KeyboardInterrupt):
        raise _AbortError from None


# ═══════════════════════════════════════════════════════════════
# 向导
# ═══════════════════════════════════════════════════════════════


def _run_wizard(config: Any) -> bool:
    """纯表单向导：(b) 边界——不预设 provider、不派生 base_url/model，三项全手输。

    Returns:
        True = 已成功写盘；False = 用户取消或校验失败。
    """
    path = config.USER_CONFIG_PATH
    bar = _sym("─", "-") * 60
    _say()
    _say(bar)
    _say("  首次配置向导：需要 LLM 连接信息（三项均为必填）")
    _say(f"  将写入：{path}")
    _say("  密钥一项输入时不显示；任意一项直接回车留空 = 取消")
    _say(bar)
    _say()

    # Windows：bash 工具强制依赖 Git Bash（不降级 cmd/PowerShell）。
    # 缺了不影响本次配置，但配置完 bash 工具仍然跑不了——提前告知，免得到时困惑。
    try:
        from fp_core.platform_utils import check_git_bash, is_windows

        if is_windows():
            _bash_ok, _bash_detail = check_git_bash()
            if not _bash_ok:
                _say(f"  {_sym('⚠', '!')} {_bash_detail}")
                _say("    （配置可继续；但 bash 工具需 Git Bash 才能执行命令）")
                _say()
    except Exception:  # noqa: BLE001 — 提示性检查，失败不影响配置主流程
        pass

    try:
        api_key = _read_secret("① API 密钥").strip()
        base_url = _read_text("② API 基础地址（例：https://api.deepseek.com/v1）").strip()
        model = _read_text("③ 模型名称（例：deepseek-chat）").strip()
    except _AbortError:
        _say("\n已取消配置。")
        return False

    fields = (("API 密钥", api_key), ("API 基础地址", base_url), ("模型名称", model))
    missing = [name for name, value in fields if not value]
    if missing:
        _say(f"\n以下项为空，配置未完成：{'、'.join(missing)}")
        return False

    # 保留既有键（如 tui 外观段），仅覆盖三键 —— config.json 只记录用户显式设置
    data: dict[str, Any] = dict(getattr(config, "_json_cfg", {}) or {})
    data["LLM_API_KEY"] = api_key
    data["LLM_API_BASE_URL"] = base_url
    data["LLM_MODEL"] = model

    try:
        _atomic_write_json(path, data)
    except OSError as exc:
        _say(f"\n写入配置文件失败：{exc}")
        return False

    _say(f"\n{_sym('✓', '[OK]')} 已写入 {path}")
    return True


def _print_guide(config: Any) -> None:
    """非交互退路的打印式引导块（面向终端用户，含可执行指引）。"""
    bar = "=" * 64
    _say()
    _say(bar)
    _say("  FP 尚未完成配置，无法启动。")
    _say(bar)
    _say()
    _say(config.llm_setup_hint())
    _say()


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════


def ensure_llm_configured(
    *,
    mode: str = "cli",
    passthrough: Iterable[str] = (),
    skip: bool = False,
) -> None:
    """门禁：确保 LLM 配置就绪，否则引导或退出。

    Args:
        mode: 启动模式（cli/webui/acp）。非 cli 不进入交互向导。
        passthrough: 透传给前端的剩余 argv（用于识别 --headless / --message）。
        skip: True 时完全跳过（--version / --update 等无需配置的路径）。

    Raises:
        SystemExit: 配置缺失且无法交互补全时以退出码 1 结束。
    """
    if skip:
        return

    import fp_core.config as config

    if config.check_llm_config(quiet=True):
        return

    _ensure_utf8()

    if _is_interactive(mode, passthrough) and _run_wizard(config):
        config = importlib.reload(config)  # 写盘后重载，本次启动即生效
        if config.check_llm_config(quiet=True):
            _say(f"{_sym('✓', '[OK]')} 配置已就绪，继续启动。\n")
            return

    _print_guide(config)
    sys.exit(1)
