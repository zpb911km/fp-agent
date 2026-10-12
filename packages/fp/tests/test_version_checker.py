"""version_checker 行为锁定 — 两条硬约束

1. 更新只作用于本地已装组件；单包 pip 失败不阻塞其余包（旧版 fp-agent[all]
   整批安装、一包失败全体回退 → 一个版本都升不上去）。
2. 提示渲染（take_notice）只读本地缓存、零网络；后台检查不打印（不砸屏）。
"""

import json

import fp.version_checker as vc


def _install_map(monkeypatch, mapping: dict[str, str]):
    monkeypatch.setattr(vc, "_get_installed_version", lambda p: mapping.get(p))


# ── do_update：只升级已装包，单包失败不阻塞 ──────────────


def test_do_update_skips_missing_packages(monkeypatch, tmp_path, capsys):
    """只装了 fp-agent/fp-core 时：不强装 fp-terminal/fp-webui/fp-acp。"""
    _install_map(monkeypatch, {"fp-agent": "0.2.0", "fp-core": "0.2.0"})
    monkeypatch.setattr(vc, "_fetch_latest_map", lambda names: {p: "0.3.0" for p in names})
    pip_calls: list[str] = []

    def fake_pip(p: str) -> tuple[bool, str]:
        pip_calls.append(p)
        return True, ""

    monkeypatch.setattr(vc, "_pip_upgrade", fake_pip)
    monkeypatch.setattr(vc, "_cache_path", lambda: str(tmp_path / "cache.json"))

    assert vc.do_update() == 0
    assert set(pip_calls) == {"fp-agent", "fp-core"}  # 缺失的 3 包一次都没碰
    out = capsys.readouterr().out
    assert "fp-webui" in out and "保持原样" in out  # 有告知，但不安装


def test_do_update_single_failure_does_not_block_others(monkeypatch, tmp_path, capsys):
    """fp-agent 升级失败时，fp-core 仍照常升级；退出码非 0 且给出重试命令。"""
    _install_map(monkeypatch, {"fp-agent": "0.2.0", "fp-core": "0.2.0", "fp-terminal": "0.2.0"})
    monkeypatch.setattr(vc, "_fetch_latest_map", lambda names: {p: "0.3.0" for p in names})
    pip_calls: list[str] = []

    def fake_pip(p: str) -> tuple[bool, str]:
        pip_calls.append(p)
        return (False, "ERROR: no matching distribution") if p == "fp-agent" else (True, "")

    monkeypatch.setattr(vc, "_pip_upgrade", fake_pip)
    monkeypatch.setattr(vc, "_cache_path", lambda: str(tmp_path / "cache.json"))

    assert vc.do_update() == 1
    assert set(pip_calls) == {"fp-agent", "fp-core", "fp-terminal"}  # 失败不阻塞后续
    out = capsys.readouterr().out
    assert "fp-agent" in out
    assert "pip install -U fp-agent" in out  # 重试命令用 PyPI 包名
    assert "更新完成" not in out


def test_do_update_all_current_is_ok(monkeypatch, tmp_path, capsys):
    _install_map(monkeypatch, {"fp-agent": "0.3.0"})
    monkeypatch.setattr(vc, "_fetch_latest_map", lambda names: {p: "0.3.0" for p in names})
    monkeypatch.setattr(vc, "_pip_upgrade", lambda p: (_ for _ in ()).throw(AssertionError("不该调 pip")))
    monkeypatch.setattr(vc, "_cache_path", lambda: str(tmp_path / "cache.json"))

    assert vc.do_update() == 0
    assert "均为最新" in capsys.readouterr().out


def test_do_update_network_down_returns_nonzero(monkeypatch, tmp_path, capsys):
    """查询全失败（离线）：不误报「已最新」，非 0 退出。"""
    _install_map(monkeypatch, {"fp-agent": "0.2.0"})
    monkeypatch.setattr(vc, "_fetch_latest_map", lambda names: {})
    monkeypatch.setattr(vc, "_cache_path", lambda: str(tmp_path / "cache.json"))

    assert vc.do_update() == 1
    out = capsys.readouterr().out
    assert "查询失败" in out
    assert "均为最新" not in out


def test_do_update_no_pip_install_hint(monkeypatch, capsys):
    """环境里一个 fp 组件都没装（源码运行）：不碰 pip，给出指引。"""
    _install_map(monkeypatch, {})
    monkeypatch.setattr(vc, "_pip_upgrade", lambda p: (_ for _ in ()).throw(AssertionError("不该调 pip")))

    assert vc.do_update() == 1
    assert "未检测到" in capsys.readouterr().out


# ── take_notice：零网络的安全点提示 ──────────────────────


def test_take_notice_reads_cache_only(monkeypatch, tmp_path, capsys):
    _install_map(monkeypatch, {"fp-agent": "0.2.0", "fp-core": "0.2.0"})
    cache = tmp_path / "version_cache.json"
    cache.write_text(json.dumps({"checked_at": 9_999_999_999.0, "latest": {"fp-agent": "0.3.0"}}))
    monkeypatch.setattr(vc, "_cache_path", lambda: str(cache))
    monkeypatch.setattr(vc, "_fetch_latest", lambda p: (_ for _ in ()).throw(AssertionError("take_notice 不许联网")))

    notice = vc.take_notice()
    assert notice is not None
    assert "fp 0.2.0 → 0.3.0" in notice
    assert "fp --update" in notice
    assert "\033[" not in notice  # 无 ANSI，样式交给前端


def test_take_notice_none_when_no_cache(monkeypatch, tmp_path):
    _install_map(monkeypatch, {"fp-agent": "0.2.0"})
    monkeypatch.setattr(vc, "_cache_path", lambda: str(tmp_path / "missing.json"))
    assert vc.take_notice() is None


def test_take_notice_none_when_up_to_date(monkeypatch, tmp_path):
    _install_map(monkeypatch, {"fp-agent": "0.3.0"})
    cache = tmp_path / "version_cache.json"
    cache.write_text(json.dumps({"checked_at": 9_999_999_999.0, "latest": {"fp-agent": "0.3.0"}}))
    monkeypatch.setattr(vc, "_cache_path", lambda: str(cache))
    assert vc.take_notice() is None


def test_take_notice_never_lists_missing_packages(monkeypatch, tmp_path):
    """缓存里有未安装组件的最新版 → 不提示、更不暗示去装它。"""
    _install_map(monkeypatch, {"fp-agent": "0.2.0"})
    cache = tmp_path / "version_cache.json"
    cache.write_text(json.dumps({"checked_at": 9_999_999_999.0, "latest": {"fp-webui": "0.9.0"}}))
    monkeypatch.setattr(vc, "_cache_path", lambda: str(cache))
    assert vc.take_notice() is None


# ── 后台检查：不打印、TTL 内不联网 ──────────────────────


def test_background_check_prints_nothing(monkeypatch, capsys):
    """后台检查绝不能直接 print（会砸 TUI 屏 / 打断输入行）。"""
    monkeypatch.setattr(vc, "_refresh_cache", lambda: True)
    vc.check_updates_background()
    assert capsys.readouterr().out == ""


def test_refresh_cache_skips_within_ttl(monkeypatch, tmp_path):
    """缓存 24h 内：不发起任何网络请求。"""
    _install_map(monkeypatch, {"fp-agent": "0.2.0"})
    cache = tmp_path / "version_cache.json"
    cache.write_text(json.dumps({"checked_at": 9_999_999_999.0, "latest": {}}))
    monkeypatch.setattr(vc, "_cache_path", lambda: str(cache))
    monkeypatch.setattr(vc, "_fetch_latest", lambda p: (_ for _ in ()).throw(AssertionError("TTL 内不许联网")))

    assert vc._refresh_cache() is False


def test_refresh_cache_fetches_and_persists_when_stale(monkeypatch, tmp_path):
    _install_map(monkeypatch, {"fp-agent": "0.2.0"})
    cache = tmp_path / "version_cache.json"
    cache.write_text(json.dumps({"checked_at": 0, "latest": {"fp-core": "0.1.0"}}))
    monkeypatch.setattr(vc, "_cache_path", lambda: str(cache))
    monkeypatch.setattr(vc, "_fetch_latest", lambda p: "0.3.0")

    assert vc._refresh_cache() is True
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["latest"]["fp-agent"] == "0.3.0"
    assert data["latest"]["fp-core"] == "0.1.0"  # 未重取的条目保留


# ── 版本比较 ─────────────────────────────────────────


def test_newer_pep440_and_numeric():
    assert vc._newer("0.3.0", "0.2.9")
    assert not vc._newer("0.3.0", "0.3.0")
    assert vc._newer("0.10.0", "0.9.9")  # 数值比较而非字符串比较
