"""test_option.py — /option 命令三来源（fetched/public/private）适配测试

背景：资产分发系统将用户目录从平铺改为三来源
  {DATA}/fetched / {DATA}/public / {DATA}/private
option.py 曾停留在老单目录扫描，导致三来源下的 disabled 扩展不显示、
enable/disable 找不到文件。本测试锁定三来源行为：
  - 扫描：三类来源的 disabled 项均能列出，来源分类正确
  - 操作：enable/disable 命中最高优先级（private 优先）版本
  - 只读：fetched 外来资产禁止 enable/disable
"""

import types
from pathlib import Path

import pytest

from fp_core.commands import option

PLUGIN_TMPL = (
    'PLUGIN_DEFINITION = {"function": {"name": "%s", "description": "%s", '
    '"parameters": {"type": "object", "properties": {}}}}\n'
    "def execute(params):\n    return 'ok'\n"
)
COMMAND_TMPL = 'name = "%s"\ndescription = "%s"\n'


class _FakeRegistry:
    _core_defs = [
        {"function": {"name": "bash", "description": "Run a shell command. extra"}},
    ]
    # 模拟 public/tools/extensions/weather_plugin.py 已被加载进注册表
    # （source 是文件基名 weather_plugin，非工具名 weather）
    _plugins = {
        "weather": {
            "definition": {
                "function": {
                    "name": "weather",
                    "description": "Weather. extra",
                    "parameters": {"type": "object", "properties": {}},
                }
            },
            "source": "weather_plugin",
            "executor": None,
        }
    }


class _FakePlugins:
    def list_plugins(self):
        return []

    def get(self, name):
        return None


class _FakeState:
    def __init__(self):
        self.tool_exec = types.SimpleNamespace(registry=_FakeRegistry())
        self.plugins = _FakePlugins()
        self.lifecycle = None


@pytest.fixture
def three_source_env(tmp_path, monkeypatch):
    """构造临时三来源目录并指向 option / config 数据根。

    布局：
      private/tools/extensions/web_search_plugin.py.disabled
      public/tools/extensions/weather_plugin.py
      fetched/tools/extensions/alien_plugin.py.disabled
      private/commands/mycustom.py.disabled
      private/plugins/tool_audit.py.disabled
    """
    root = Path(tmp_path)
    (root / "private/tools/extensions").mkdir(parents=True, exist_ok=True)
    (root / "public/tools/extensions").mkdir(parents=True, exist_ok=True)
    (root / "fetched/tools/extensions").mkdir(parents=True, exist_ok=True)
    (root / "private/plugins").mkdir(parents=True, exist_ok=True)
    (root / "private/commands").mkdir(parents=True, exist_ok=True)

    (root / "private/tools/extensions/web_search_plugin.py.disabled").write_text(
        PLUGIN_TMPL % ("web_search", "Web search"), encoding="utf-8"
    )
    (root / "public/tools/extensions/weather_plugin.py").write_text(
        PLUGIN_TMPL % ("weather", "Weather"), encoding="utf-8"
    )
    (root / "fetched/tools/extensions/alien_plugin.py.disabled").write_text(
        PLUGIN_TMPL % ("alien", "Alien"), encoding="utf-8"
    )
    (root / "private/commands/mycustom.py.disabled").write_text(
        COMMAND_TMPL % ("mycustom", "My custom command"), encoding="utf-8"
    )
    (root / "private/plugins/tool_audit.py.disabled").write_text(
        COMMAND_TMPL % ("tool_audit", "Tool audit"), encoding="utf-8"
    )
    # 用户覆盖内置命令 history（diff 场景）
    (root / "private/commands/history.py").write_text(
        'name = "history"\ndescription = "History (user override)"\n', encoding="utf-8"
    )

    # 指向临时根（import 时计算的模块级常量需显式覆盖）
    monkeypatch.setattr(option, "DATA_DIR", str(root))
    import fp_core.config as cfg

    monkeypatch.setattr(cfg, "_FP_DATA_DIR", str(root))
    return root


def _manager():
    return option.OptionManager(_FakeState())


# ── 扫描 ───────────────────────────────────────────────────────


def test_scan_shows_disabled_from_all_three_sources(three_source_env):
    """三来源中的 disabled 扩展都应被列出，且来源分类正确。"""
    mgr = _manager()
    disabled = {it.name: it for it in mgr.scan_all() if it.status == "disabled"}

    assert "web_search" in disabled  # private/tools
    assert "tool_audit" in disabled  # private/plugins
    assert "mycustom" in disabled  # private/commands
    assert "alien" in disabled  # fetched

    assert disabled["web_search"].source == "private"
    assert disabled["tool_audit"].source == "private"
    assert disabled["alien"].source == "fetched"


def test_scan_enabled_from_public(three_source_env):
    """public 来源的 enabled 工具应能找到其源文件路径（而非 [core]）。"""
    mgr = _manager()
    items = {it.name: it for it in mgr.scan_all() if it.type == "tool"}
    weather = items.get("weather")
    assert weather is not None
    assert weather.source == "public"
    assert weather.status == "enabled"
    assert weather.source_path.endswith("weather_plugin.py")


# ── 查找与操作 ─────────────────────────────────────────────────


def test_find_prefers_private_over_lower_priority(three_source_env):
    """同名多来源存在时，查找命中高优先级（private）。"""
    mgr = _manager()
    # 在 public 也放一份同名 disabled，private 应胜出
    pub = three_source_env / "public/tools/extensions/web_search_plugin.py.disabled"
    pub.write_text(PLUGIN_TMPL % ("web_search", "Web search public"), encoding="utf-8")

    found = mgr._find_disabled("web_search")
    assert "private" in found
    assert "public" not in found


def test_enable_from_private(three_source_env):
    mgr = _manager()
    ok, msg = mgr.enable("web_search")
    assert ok, msg
    assert (three_source_env / "private/tools/extensions/web_search_plugin.py").exists()
    assert not (three_source_env / "private/tools/extensions/web_search_plugin.py.disabled").exists()


def test_disable_to_public(three_source_env):
    mgr = _manager()
    ok, msg = mgr.disable("weather")
    assert ok, msg
    assert (three_source_env / "public/tools/extensions/weather_plugin.py.disabled").exists()


def test_enable_command_from_private(three_source_env):
    mgr = _manager()
    ok, msg = mgr.enable("mycustom")
    assert ok, msg
    assert (three_source_env / "private/commands/mycustom.py").exists()


# ── fetched 只读保护 ───────────────────────────────────────────


def test_cannot_enable_fetched_asset(three_source_env):
    mgr = _manager()
    ok, msg = mgr.enable("alien")
    assert not ok
    assert "只读" in msg
    assert (three_source_env / "fetched/tools/extensions/alien_plugin.py.disabled").exists()


def test_cannot_disable_fetched_asset(three_source_env):
    # 把 alien 改为 enabled 状态，disable 应同样被拒
    src = three_source_env / "fetched/tools/extensions/alien_plugin.py.disabled"
    src.rename(three_source_env / "fetched/tools/extensions/alien_plugin.py")
    mgr = _manager()
    ok, msg = mgr.disable("alien")
    assert not ok
    assert "只读" in msg
    assert (three_source_env / "fetched/tools/extensions/alien_plugin.py").exists()


# ── 来源分类 ───────────────────────────────────────────────────


def test_classify_source(three_source_env):
    root = three_source_env
    assert option.OptionManager._classify_source(_manager(), str(root / "private/tools/x.py")) == "private"
    assert option.OptionManager._classify_source(_manager(), str(root / "public/tools/x.py")) == "public"
    assert option.OptionManager._classify_source(_manager(), str(root / "fetched/tools/x.py")) == "fetched"
    # 内置路径
    import fp_core

    assert option.OptionManager._classify_source(_manager(), str(fp_core.__file__)) == "builtin"


# ── diff ───────────────────────────────────────────────────────


def test_diff_finds_three_source_user_version(three_source_env):
    """diff 应在三来源中找到用户覆盖版本（private/commands/history.py 覆盖内置）。"""
    mgr = _manager()
    result = mgr.diff("history")
    assert result is not None
    assert "user_path" in result
    assert result["user_path"].endswith("history.py")
    assert "private" in result["user_path"]
    assert result["builtin_path"].endswith("commands/history.py")
