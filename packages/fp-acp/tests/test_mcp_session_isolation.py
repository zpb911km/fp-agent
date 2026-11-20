"""MCP 会话隔离的真实机制测试（任务 #248 第 1 项）。

`test_acp_wire.py` 用 `_FakeMcpPlugin` 只验了「ACP 侧有没有带 sid 登记」，
**没有验证隔离本身** —— 那才是方案 B 的全部内容。这里直接加载真实的私有插件
（`private/plugins/mcp.py`），用执行期 `current_state` 拨动会话导线，断言
`mcp_list` / `mcp_call` 看到的 server 集合随会话变化。

为什么按路径加载而不是 import：私有插件不随代码分发，机器上没有时本文件整体
skip（`pytest.skip` 带原因），不把「本地有私有插件」当成对所有人的要求。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fp_core.core import state as state_mod
from fp_core.platform_utils import get_data_dir


def _plugin_path() -> Path:
    """定位私有插件路径。

    根 conftest 把 ``XDG_DATA_HOME`` 钉进临时目录（见 ``docs/dev/测试.md``
    § 隔离用户目录），所以 ``get_data_dir()`` 在这里**看不到**用户真实插件。
    但本文件的语义恰是「本机装了私有插件就测真实实现，没装就跳过」，隔离不该
    把整组用例变成恒 skip。conftest 钉住前已把真实数据根导出到
    ``FP_TEST_REAL_DATA_DIR``（同 fp-core/tests/test_subagent_asset.py 的用法）。
    """
    real = os.environ.get("FP_TEST_REAL_DATA_DIR")
    if real:
        return Path(real) / "private" / "plugins" / "mcp.py"
    return Path(get_data_dir()) / "private" / "plugins" / "mcp.py"


_PLUGIN_PATH = _plugin_path()


def _load_plugin_module() -> Any:
    """按文件路径加载私有 mcp 插件模块（不存在则 skip 整个文件）。"""
    if not _PLUGIN_PATH.is_file():
        pytest.skip(f"本机无私有插件 {_PLUGIN_PATH}（不随代码分发）", allow_module_level=True)
    spec = importlib.util.spec_from_file_location("_fp_private_mcp_under_test", _PLUGIN_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


MCP = _load_plugin_module()


class _FakeHandle:
    """McpServerHandle 的替身：不启子进程，只记录生命周期。"""

    instances: list[_FakeHandle] = []

    def __init__(self, name: str, command: list[str], env: dict[str, str] | None = None) -> None:
        self.name = name
        self.command = list(command)
        self.env = dict(env or {})
        self.killed = False
        _FakeHandle.instances.append(self)

    async def list_tools(self) -> list[dict[str, Any]]:
        return [{"name": f"{self.name}_tool", "description": "d"}]

    async def call_tool(self, tool: str, args: dict[str, Any]) -> str:
        return f"{self.name}:{tool}"

    def kill(self) -> None:
        self.killed = True


@pytest.fixture(autouse=True)
def fake_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeHandle.instances.clear()
    monkeypatch.setattr(MCP, "McpServerHandle", _FakeHandle)


@pytest.fixture
def plug(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """干净插件实例：全局 server 为空，servers.json 指向 tmp（不读用户真实配置）。"""
    cfg = tmp_path / "servers.json"
    cfg.write_text('{"servers": {}}')
    monkeypatch.setattr(MCP, "SERVERS_PATH", str(cfg))
    return MCP.create_plugin()


@pytest.fixture
def as_session() -> Any:
    """拨动执行期会话导线；退出时复位（contextvar 会跨测试残留）。"""

    def _set(sid: str | None) -> None:
        if sid is None:
            state_mod.current_state.set(None)
        else:
            state_mod.current_state.set(SimpleNamespace(session_id=sid))

    yield _set
    state_mod.current_state.set(None)


# ═══════════════════════════════════════════════════════════
# 隔离本身
# ═══════════════════════════════════════════════════════════


def test_session_servers_are_invisible_to_other_sessions(plug: Any, as_session: Any) -> None:
    """核心不变量：A 会话登记的 server，B 会话看不到，A 自己看得到。"""
    plug.add_session_servers("A", {"fs": {"command": "npx"}})

    as_session("B")
    assert plug._visible() == {}, "B 能看见 A 的 server = 隔离没生效（退回全局）"

    as_session("A")
    assert set(plug._visible()) == {"fs"}


def test_session_servers_do_not_leak_into_globally_visible_set(plug: Any, as_session: Any) -> None:
    """私有登记不得写进 `_servers`（那是全局面）。"""
    plug.add_session_servers("A", {"fs": {"command": "npx"}})
    assert plug._servers == {}, "私有 server 被塞进全局字典 = 所有会话都能用"


def test_two_sessions_can_use_same_name_without_collision(plug: Any, as_session: Any) -> None:
    """同名 server 在两个会话里各自独立（否则 A 的配置会被 B 覆盖）。"""
    plug.add_session_servers("A", {"fs": {"command": "npx", "args": ["-a"]}})
    plug.add_session_servers("B", {"fs": {"command": "uvx", "args": ["-b"]}})

    as_session("A")
    a_cmd = plug._visible()["fs"].command
    as_session("B")
    b_cmd = plug._visible()["fs"].command
    assert a_cmd == ["npx", "-a"]
    assert b_cmd == ["uvx", "-b"]


def test_no_bound_session_sees_only_global(plug: Any, as_session: Any) -> None:
    """无会话导线（CLI/后台任务）时不暴露任何私有 server，也不崩。"""
    plug.add_session_servers("A", {"fs": {"command": "npx"}})
    as_session(None)
    assert plug._visible() == {}


# ═══════════════════════════════════════════════════════════
# 工具面：mcp_list / mcp_call 按会话过滤
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mcp_list_shows_only_current_session_servers(plug: Any, as_session: Any) -> None:
    import json

    plug.add_session_servers("A", {"fs": {"command": "npx"}})
    as_session("B")
    assert json.loads(await plug._exec_mcp_list({}))["mcp_servers"] == {}

    as_session("A")
    got = json.loads(await plug._exec_mcp_list({}))
    assert set(got["mcp_servers"]) == {"fs"}


@pytest.mark.asyncio
async def test_mcp_call_refuses_other_sessions_server(plug: Any, as_session: Any) -> None:
    """模型在 B 会话里点名 A 的 server，必须拒绝（否则隔离只在展示层）。"""
    plug.add_session_servers("A", {"fs": {"command": "npx"}})
    as_session("B")
    out = await plug._exec_mcp_call({"server": "fs", "tool": "read"})
    # 断言「说清是会话可见性问题」而不只是「拒绝了」：同名 server 在别的会话里存在时，
    # 报「未配置」会把模型/用户引向去改 servers.json（白费功夫），诊断性才是这层的要求。
    assert "属于其它会话" in out, f"应说明是会话可见性问题，实际: {out!r}"

    as_session("A")
    assert await plug._exec_mcp_call({"server": "fs", "tool": "read"}) == "fs:read"


# ═══════════════════════════════════════════════════════════
# 回收与输入校验
# ═══════════════════════════════════════════════════════════


def test_drop_session_servers_kills_processes(plug: Any, as_session: Any) -> None:
    """会话关闭必须真的杀子进程（否则每次开 thread 都漏一个进程）。"""
    plug.add_session_servers("A", {"fs": {"command": "npx"}, "gh": {"command": "npx"}})
    handles = [h for h in _FakeHandle.instances if h.name in ("fs", "gh")]
    assert len(handles) == 2

    plug.drop_session_servers("A")
    assert all(h.killed for h in handles), "handle 未 kill = 进程泄漏"
    as_session("A")
    assert plug._visible() == {}


def test_drop_unknown_session_is_noop(plug: Any) -> None:
    """session/close 幂等：回收不存在的会话不报错。"""
    plug.drop_session_servers("nope")  # 不抛即通过


def test_reregistering_same_name_kills_old_handle(plug: Any) -> None:
    """重复登记同名 server（如 session/load 再来一次）要先杀旧进程。"""
    plug.add_session_servers("A", {"fs": {"command": "npx"}})
    first = _FakeHandle.instances[-1]
    plug.add_session_servers("A", {"fs": {"command": "npx"}})
    assert first.killed, "旧 handle 未回收 = 每次 load 漏一个进程"


def test_non_stdio_transport_is_rejected_not_silently_dropped(plug: Any) -> None:
    """http/sse 型（无 command）要明确报告拒绝，不能静默忽略。"""
    rejected = plug.add_session_servers("A", {"remote": {"url": "https://example.com/mcp", "type": "http"}})
    assert rejected and "remote" in rejected[0]
    assert "stdio" in rejected[0], "拒绝原因要说明只支持 stdio"
    assert plug._session_servers.get("A") == {}


def test_malformed_config_is_rejected(plug: Any) -> None:
    rejected = plug.add_session_servers("A", {"bad": "just-a-string"})
    assert rejected and "bad" in rejected[0]


def test_string_command_with_args_is_split_correctly(plug: Any) -> None:
    """ACP 传 `{"command": "npx", "args": [...]}`，不是拼接好的字符串。"""
    plug.add_session_servers("A", {"fs": {"command": "npx", "args": ["-y", "srv"]}})
    assert _FakeHandle.instances[-1].command == ["npx", "-y", "srv"]


def test_env_is_passed_through(plug: Any) -> None:
    plug.add_session_servers("A", {"fs": {"command": "npx", "env": {"TOKEN": "t"}}})
    assert _FakeHandle.instances[-1].env == {"TOKEN": "t"}


def test_plugin_loads_without_servers_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """servers.json 缺失时插件仍可实例化（元入口可用、servers 为空）。"""
    monkeypatch.setattr(MCP, "SERVERS_PATH", os.path.join(str(tmp_path), "missing.json"))
    p = MCP.create_plugin()
    assert p._servers == {}
