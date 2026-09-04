"""register_command / unregister_command 生命周期测试

回归锚点（反思 #8）：命令注册表此前"只注册不注销"——插件动态注入的
/sc 在插件禁用后残留；且 unregister 若按"模块名相等"判断会误删同名
文件命令（如 help），必须以动态登记簿 _dynamic_names 区分。
"""

from types import SimpleNamespace

import fp_core.commands as cmd_mod
from fp_core.commands import get_command, register_command, unregister_command


def _fake_module(name: str = "dyn", aliases: list[str] | None = None):
    mod = SimpleNamespace(name=name, description="dyn test", aliases=aliases or [])
    return mod


def _snapshot():
    return (
        dict(cmd_mod._commands),
        set(cmd_mod._dynamic_names),
    )


def _restore(snap):
    cmd_mod._commands, cmd_mod._dynamic_names = snap


def test_register_and_unregister_roundtrip():
    snap = _snapshot()
    try:
        mod = _fake_module()
        register_command("dyn", mod)
        assert get_command("dyn") is mod
        unregister_command("dyn")
        assert get_command("dyn") is None
    finally:
        _restore(snap)


def test_unregister_removes_aliases_too():
    snap = _snapshot()
    try:
        mod = _fake_module(aliases=["d1", "d2"])
        register_command("dyn", mod)
        assert get_command("d1") is mod and get_command("d2") is mod
        unregister_command("dyn")
        assert get_command("dyn") is None
        assert get_command("d1") is None
        assert get_command("d2") is None
    finally:
        _restore(snap)


def test_unregister_protects_file_commands():
    """同名的自动发现文件命令不应被 unregister 误删"""
    snap = _snapshot()
    try:
        # help 是内置文件命令（存在性由本 repo commands/ 保证）
        assert get_command("help") is not None, "前置：内置 help 命令应在"
        unregister_command("help")
        assert get_command("help") is not None, "文件命令必须受保护"
    finally:
        _restore(snap)


def test_unregister_unknown_is_noop():
    snap = _snapshot()
    try:
        unregister_command("no_such_cmd_zzz")  # 不应抛异常
        unregister_command("dyn")  # 从未注册，也应 no-op
    finally:
        _restore(snap)


def test_recover_dynamic_registry_after_rediscover():
    """重新 _discover 后动态登记簿被清空，旧名 unregister 为幂等 no-op"""
    snap = _snapshot()
    try:
        mod = _fake_module()
        register_command("dyn", mod)
        cmd_mod._discover_commands()
        assert get_command("dyn") is None  # 动态注册不随重新发现存活
        unregister_command("dyn")  # 登记簿已清 → no-op，不抛异常
    finally:
        _restore(snap)


def test_shortcircuit_on_unregister_cleans_sc_command():
    """shortcircuit 插件禁用（on_unregister）必须清理其注入的 /sc 命令"""
    from fp_core.plugins.shortcircuit import command as sc_command
    from fp_core.plugins.shortcircuit.plugin import ShortcircuitPlugin

    snap = _snapshot()
    try:
        # 模拟 ON_INIT 已注入 /sc
        register_command("sc", sc_command)
        plugin = ShortcircuitPlugin()
        plugin._command_injected = True

        plugin.on_unregister()

        assert get_command("sc") is None, "插件禁用后 /sc 必须被清理"
        assert plugin._command_injected is False
    finally:
        _restore(snap)
