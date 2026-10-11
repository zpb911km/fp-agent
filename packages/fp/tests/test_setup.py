"""测试 fp.setup — 首跑配置门禁与引导向导（无网络、无真实 tty）。"""

import json
import os
import stat
import sys
import tempfile

import pytest

# 配置/数据目录隔离（跨平台）：POSIX 走 XDG_*，Windows 走 APPDATA/LOCALAPPDATA
# （XDG_* 在 Windows 被 platform_utils 忽略）。指向系统临时目录，测试不碰真实用户配置。
os.environ.setdefault("XDG_CONFIG_HOME", os.path.join(tempfile.gettempdir(), "fp_test_config"))
os.environ.setdefault("XDG_DATA_HOME", os.path.join(tempfile.gettempdir(), "fp_test_data"))
os.environ.setdefault("APPDATA", os.path.join(tempfile.gettempdir(), "fp_test_appdata"))
os.environ.setdefault("LOCALAPPDATA", os.path.join(tempfile.gettempdir(), "fp_test_localappdata"))

import fp.setup as setup
import fp_core.config as config


class _TTY:
    def isatty(self):
        return True


class _NoTTY:
    def isatty(self):
        return False


class _FakeImportlib:
    @staticmethod
    def reload(mod):
        return mod


# ═══════════════════════════════════════════════════════════════
# 交互性判定
# ═══════════════════════════════════════════════════════════════


def test_is_interactive_requires_tty(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _TTY())
    assert setup._is_interactive("cli", []) is True
    monkeypatch.setattr(sys, "stdin", _NoTTY())
    assert setup._is_interactive("cli", []) is False


def test_is_interactive_allows_cli_and_webui(monkeypatch):
    # cli 与 webui 的 stdin 都属于用户 → 都可进向导
    monkeypatch.setattr(sys, "stdin", _TTY())
    assert setup._is_interactive("cli", []) is True
    assert setup._is_interactive("webui", []) is True


def test_is_interactive_rejects_acp(monkeypatch):
    # acp 的 stdin 是 IDE 的 JSON-RPC 协议流：即使 tty 也必须拒绝（占用即破坏协议）
    monkeypatch.setattr(sys, "stdin", _TTY())
    assert setup._is_interactive("acp", []) is False


def test_is_interactive_rejects_noninteractive_flags(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _TTY())
    assert setup._is_interactive("cli", ["--headless"]) is False
    assert setup._is_interactive("cli", ["--message", "hi"]) is False
    assert setup._is_interactive("cli", ["--message=hi"]) is False


def test_is_interactive_rejects_subagent(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _TTY())
    monkeypatch.setenv("FP_IS_SUBAGENT", "1")
    assert setup._is_interactive("cli", []) is False
    monkeypatch.delenv("FP_IS_SUBAGENT")
    monkeypatch.setenv("FP_SUBAGENT_ROLE", "{}")
    assert setup._is_interactive("cli", []) is False


# ═══════════════════════════════════════════════════════════════
# 门禁
# ═══════════════════════════════════════════════════════════════


def test_skip_bypasses_everything(monkeypatch):
    called = []
    monkeypatch.setattr(config, "check_llm_config", lambda quiet=False: called.append(1) or False)
    setup.ensure_llm_configured(mode="cli", skip=True)
    assert called == []  # --version/--update 路径不触发任何配置检查


def test_ready_passes_through(monkeypatch):
    monkeypatch.setattr(config, "check_llm_config", lambda quiet=False: True)
    setup.ensure_llm_configured(mode="cli")  # 不退出、不进向导


def test_noninteractive_missing_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(config, "check_llm_config", lambda quiet=False: False)
    monkeypatch.setattr(config, "llm_setup_hint", lambda missing=None: "HINT-LINE")
    monkeypatch.setattr(setup, "_is_interactive", lambda mode, passthrough: False)
    with pytest.raises(SystemExit) as ei:
        setup.ensure_llm_configured(mode="cli")
    assert ei.value.code == 1
    assert "HINT-LINE" in capsys.readouterr().out


def test_webui_non_tty_missing_exits_nonzero(monkeypatch, capsys):
    # webui 无 tty（被服务/IDE 拉起）时不进向导，直接非 0 退出
    monkeypatch.setattr(sys, "stdin", _NoTTY())
    monkeypatch.setattr(config, "check_llm_config", lambda quiet=False: False)
    monkeypatch.setattr(config, "llm_setup_hint", lambda missing=None: "H")
    with pytest.raises(SystemExit) as ei:
        setup.ensure_llm_configured(mode="webui")
    assert ei.value.code == 1


# ═══════════════════════════════════════════════════════════════
# 向导
# ═══════════════════════════════════════════════════════════════


def _patch_wizard(monkeypatch, cfgpath, texts):
    """把向导接到临时路径与脚本化输入上；importlib.reload 打桩避免真实重载。"""
    monkeypatch.setattr(config, "check_llm_config", lambda quiet=False: cfgpath.exists())
    monkeypatch.setattr(config, "llm_setup_hint", lambda missing=None: "HINT")
    monkeypatch.setattr(config, "USER_CONFIG_PATH", str(cfgpath), raising=False)
    monkeypatch.setattr(config, "_json_cfg", {}, raising=False)
    monkeypatch.setattr(setup, "_read_secret", lambda label: "sk-secret")
    it = iter(texts)
    monkeypatch.setattr(setup, "_read_text", lambda label: next(it))
    monkeypatch.setattr(setup, "importlib", _FakeImportlib)


def test_wizard_writes_config_with_perms(monkeypatch, tmp_path):
    cfgpath = tmp_path / "fp" / "config.json"
    _patch_wizard(monkeypatch, cfgpath, ["https://api.example.com/v1", "my-model"])
    monkeypatch.setattr(sys, "stdin", _TTY())

    setup.ensure_llm_configured(mode="cli", passthrough=[])

    data = json.loads(cfgpath.read_text(encoding="utf-8"))
    assert data == {
        "LLM_API_KEY": "sk-secret",
        "LLM_API_BASE_URL": "https://api.example.com/v1",
        "LLM_MODEL": "my-model",
    }
    if os.name != "nt":
        assert stat.S_IMODE(cfgpath.stat().st_mode) == 0o600


def test_wizard_works_for_webui_mode(monkeypatch, tmp_path):
    """webui 的 stdin 归用户 → tty 下同样能完成向导，不再是 cli 专属。"""
    cfgpath = tmp_path / "fp" / "config.json"
    _patch_wizard(monkeypatch, cfgpath, ["https://api.example.com/v1", "webui-model"])
    monkeypatch.setattr(sys, "stdin", _TTY())

    setup.ensure_llm_configured(mode="webui", passthrough=["--port", "9000"])

    data = json.loads(cfgpath.read_text(encoding="utf-8"))
    assert data["LLM_MODEL"] == "webui-model"


def test_wizard_blank_field_aborts(monkeypatch, tmp_path):
    cfgpath = tmp_path / "config.json"
    _patch_wizard(monkeypatch, cfgpath, ["", "my-model"])  # base_url 留空
    monkeypatch.setattr(sys, "stdin", _TTY())

    with pytest.raises(SystemExit) as ei:
        setup.ensure_llm_configured(mode="cli")
    assert ei.value.code == 1
    assert not cfgpath.exists()  # 未完成 → 不落盘


def test_wizard_preserves_existing_keys(monkeypatch, tmp_path):
    cfgpath = tmp_path / "config.json"
    monkeypatch.setattr(config, "check_llm_config", lambda quiet=False: cfgpath.exists())
    monkeypatch.setattr(config, "llm_setup_hint", lambda missing=None: "H")
    monkeypatch.setattr(config, "USER_CONFIG_PATH", str(cfgpath), raising=False)
    monkeypatch.setattr(config, "_json_cfg", {"tui": {"theme": "dracula"}}, raising=False)
    monkeypatch.setattr(setup, "_read_secret", lambda label: "sk")
    it = iter(["https://a/v1", "m"])
    monkeypatch.setattr(setup, "_read_text", lambda label: next(it))
    monkeypatch.setattr(setup, "importlib", _FakeImportlib)
    monkeypatch.setattr(sys, "stdin", _TTY())

    setup.ensure_llm_configured(mode="cli")

    data = json.loads(cfgpath.read_text(encoding="utf-8"))
    assert data["tui"] == {"theme": "dracula"}  # 既有键保留
    assert data["LLM_MODEL"] == "m"


def test_atomic_write_json_roundtrip_and_perms(tmp_path):
    p = tmp_path / "sub" / "c.json"
    setup._atomic_write_json(str(p), {"a": 1, "中文": "值"})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1, "中文": "值"}
    if os.name != "nt":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_config_dir_windows_branch(monkeypatch):
    """Windows 分支：配置目录 = %APPDATA%/fp（不依赖真实 os，跨平台可断言）"""
    import fp_core.platform_utils as pu

    monkeypatch.setattr(pu, "is_windows", lambda: True)
    monkeypatch.setenv("APPDATA", os.path.join("C:", "Users", "tester", "AppData", "Roaming"))
    d = pu.get_config_dir()
    assert d.endswith(os.path.join("Roaming", "fp"))
    assert "AppData" in d
