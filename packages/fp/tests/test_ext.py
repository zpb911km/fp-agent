"""fp ext — 扩展资产分发 CLI 测试

覆盖：CLI 子命令（new/list/info/promote/demote/check/migrate）+ 底层模块。
全部通过 XDG_DATA_HOME 隔离到临时目录，不触碰真实用户数据。
"""

import os

import pytest

# fp 包（editable 安装，直接 import）
from fp.ext import ext_main
from fp.ext_assets import detect_legacy, source_dir, source_root
from fp.ext_manifest import parse_fp_manifest, parse_frontmatter, validate_manifest
from fp.ext_scanner import scan_file
from fp.ext_store import append_audit, load_audit, load_registry
from fp_core.config import user_dirs


@pytest.fixture(autouse=True)
def _isolate_data(tmp_path, monkeypatch):
    """隔离 XDG_DATA_HOME 到临时目录。"""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    # fp_core.config._FP_DATA_DIR 在 import 时已绑定，需重置
    from fp_core import platform_utils

    monkeypatch.setattr(platform_utils, "get_data_dir", lambda: os.path.join(str(tmp_path), "fp"))
    # 重新绑定 config 的 _FP_DATA_DIR（config 模块缓存了旧值）
    import fp_core.config as cfg

    monkeypatch.setattr(cfg, "_FP_DATA_DIR", os.path.join(str(tmp_path), "fp"))
    return tmp_path


def _run(*argv: str) -> int:
    """运行 ext_main，返回退出码。"""
    return ext_main(list(argv))


# ═══════════════════════════════════════════════════════════
# 底层模块
# ═══════════════════════════════════════════════════════════


class TestManifest:
    def test_parse_fp_manifest(self, tmp_path):
        f = tmp_path / "demo.py"
        f.write_text('__fp__ = {"name": "demo", "version": "1.0.0", "type": "tools"}\n', encoding="utf-8")
        m = parse_fp_manifest(str(f))
        assert m == {"name": "demo", "version": "1.0.0", "type": "tools"}

    def test_parse_fp_manifest_no_exec(self, tmp_path):
        """恶意文件：__fp__ 中包含代码调用 → 不执行，返回 None"""
        f = tmp_path / "evil.py"
        f.write_text('__fp__ = __import__("os").system("rm -rf /")\n', encoding="utf-8")
        m = parse_fp_manifest(str(f))
        assert m is None

    def test_parse_frontmatter(self):
        content = "---\nname: x\ndescription: desc\ntype: skill\n---\n\nbody\n"
        fm = parse_frontmatter(content)
        assert fm["name"] == "x"
        assert fm["type"] == "skill"

    def test_validate_manifest_missing_name(self):
        issues = validate_manifest({"type": "tools"}, "tools")
        assert any("name" in i for i in issues)

    def test_validate_manifest_type_mismatch(self):
        issues = validate_manifest({"name": "a", "type": "commands"}, "tools")
        assert any("type" in i for i in issues)


class TestScanner:
    def test_scan_clean(self, tmp_path):
        f = tmp_path / "ok.py"
        f.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        assert scan_file(str(f)) == []

    def test_scan_evil(self, tmp_path):
        f = tmp_path / "evil.py"
        f.write_text('import os\nos.system("rm -rf /")\n', encoding="utf-8")
        hits = scan_file(str(f))
        rules = {h.rule_id for h in hits}
        assert "code-exec" in rules
        assert "exec-import" in rules

    def test_scan_promote_privacy(self, tmp_path):
        f = tmp_path / "leak.py"
        f.write_text('path = "/home/zpb/.ssh/id_rsa"\n', encoding="utf-8")
        hits = scan_file(str(f), scene="promote")
        assert any(h.rule_id == "privacy" for h in hits)
        # install 场景不报隐私
        assert scan_file(str(f)) == []


class TestStore:
    def test_registry_roundtrip(self):
        append_audit("test", "tools/x")
        recs = load_audit()
        assert recs[-1]["action"] == "test"
        assert recs[-1]["asset"] == "tools/x"


# ═══════════════════════════════════════════════════════════
# CLI 子命令
# ═══════════════════════════════════════════════════════════


class TestReviewGate:
    """三阶段门禁：fetch → review(approve/reject) → install。"""

    def _make_local_pkg(self, tmp_path, name="hello"):
        pkg = tmp_path / f"{name}_pkg"
        pkg.mkdir()
        (pkg / f"{name}_plugin.py").write_text(
            f'__fp__ = {{"name": "{name}", "version": "0.1.0", "type": "tools"}}\n\ndef add(a, b):\n    return a + b\n',
            encoding="utf-8",
        )
        return str(pkg)

    def test_fetch_registers_pending(self, tmp_path):
        src = self._make_local_pkg(tmp_path)
        assert _run("fetch", src) == 0
        reg = load_registry()["assets"]
        assert "tools/hello" in reg
        assert reg["tools/hello"]["status"] == "pending_review"
        assert reg["tools/hello"]["source"]  # 来源已记录

    def test_staging_isolated_in_tmp(self, tmp_path):
        """回归防护：staging 必须落在隔离目录内，杜绝真实用户目录残留。

        历史 bug：STAGING_DIR 曾是模块级常量，import 时绑定真实目录，
        导致 fetch 测试把 staging 写到 ~/.local/share/fp/.staging/ 而 registry 写到 tmp。
        """
        src = self._make_local_pkg(tmp_path)
        assert _run("fetch", src) == 0
        # 真实用户目录不得出现新 staging
        real_staging = os.path.join(os.path.expanduser("~"), ".local", "share", "fp", ".staging", "hello")
        assert not os.path.exists(real_staging)
        # 隔离目录内必须有 staging（staging 名 = fetch 源目录名，不一定是资产名）
        isolated_staging_root = os.path.join(str(tmp_path), "fp", ".staging")
        assert os.path.isdir(isolated_staging_root)
        assert any(os.path.isdir(os.path.join(isolated_staging_root, n)) for n in os.listdir(isolated_staging_root))

    def test_install_blocked_without_review(self, tmp_path):
        src = self._make_local_pkg(tmp_path)
        _run("fetch", src)
        assert _run("install", "hello") == 1

    def test_review_approve_then_install(self, tmp_path):
        src = self._make_local_pkg(tmp_path)
        _run("fetch", src)
        assert _run("review", "hello", "--approve", "--note", "审查通过") == 0
        assert _run("install", "hello") == 0
        reg = load_registry()["assets"]["tools/hello"]
        assert reg["status"] == "active"
        assert reg["note"] == "审查通过"
        assert reg["reviewed_at"]
        # 落地到 fetched/
        fetched_dir = os.path.join(source_dir("fetched", "tools"), "hello")
        assert os.path.isdir(fetched_dir)

    def test_review_reject_then_install_blocked(self, tmp_path):
        src = self._make_local_pkg(tmp_path)
        _run("fetch", src)
        assert _run("review", "hello", "--reject", "--note", "有风险") == 0
        assert _run("install", "hello") == 1
        assert load_registry()["assets"]["tools/hello"]["status"] == "rejected"

    def test_review_audit_recorded(self, tmp_path):
        src = self._make_local_pkg(tmp_path)
        _run("fetch", src)
        _run("review", "hello", "--approve", "--note", "OK")
        recs = load_audit()
        assert any(r["action"] == "review" and r["asset"] == "tools/hello" and r["note"] == "OK" for r in recs)

    def test_remove_pending_cleans_staging(self, tmp_path):
        src = self._make_local_pkg(tmp_path)
        _run("fetch", src)
        assert _run("remove", "hello") == 0
        assert "tools/hello" not in load_registry()["assets"]

    def test_check_reports_pending(self, tmp_path, capsys):
        src = self._make_local_pkg(tmp_path)
        _run("fetch", src)
        assert _run("check") == 0
        out = capsys.readouterr().out
        assert "待安装资产" in out
        assert "待审查" in out

    def test_update_auto_reapprove(self, tmp_path):
        """已安装资产 update：同 source 重新拉取，无高危 → 自动续审并落地。"""
        src = self._make_local_pkg(tmp_path)
        _run("fetch", src)
        _run("review", "hello", "--approve", "--note", "OK")
        _run("install", "hello")
        assert _run("update", "hello") == 0
        reg = load_registry()["assets"]["tools/hello"]
        assert reg["status"] == "active"
        # 审计留痕
        recs = load_audit()
        assert any(r["action"] == "update" for r in recs)
        assert any(r["action"] == "review" and "自动续审" in r["note"] for r in recs)


class TestCli:
    def test_new_and_list(self):
        assert _run("new", "tools", "hello") == 0
        assert _run("new", "commands", "greet") == 0
        assert _run("new", "plugins", "myplugin") == 0
        assert _run("new", "memory", "memo") == 0
        # 生成后文件存在
        assert os.path.isfile(source_dir("private", "tools") + "/hello_plugin.py")
        assert os.path.isfile(source_dir("private", "commands") + "/greet.py")
        assert os.path.isdir(source_dir("private", "plugins") + "/myplugin")
        assert os.path.isfile(source_dir("private", "memory") + "/memo.md")

    def test_new_invalid_type(self):
        """argparse choices 校验非法类型 → SystemExit(2)。"""
        with pytest.raises(SystemExit):
            _run("new", "invalid", "x")

    def test_info(self, capsys):
        _run("new", "tools", "hello")
        assert _run("info", "hello") == 0
        out = capsys.readouterr().out
        assert "hello" in out

    def test_info_not_found(self):
        assert _run("info", "nonexistent") == 1

    def test_promote_demote_roundtrip(self):
        _run("new", "commands", "greet")
        # promote
        assert _run("promote", "greet") == 0
        assert not os.path.exists(source_dir("private", "commands") + "/greet.py")
        assert os.path.exists(source_dir("public", "commands") + "/greet.py")
        # demote
        assert _run("demote", "greet") == 0
        assert os.path.exists(source_dir("private", "commands") + "/greet.py")
        assert not os.path.exists(source_dir("public", "commands") + "/greet.py")

    def test_promote_fetched_rejected(self):
        """fetched 资产不能 promote（防再分发）。"""
        fetched_dir = source_dir("fetched", "commands")
        os.makedirs(fetched_dir, exist_ok=True)
        with open(os.path.join(fetched_dir, "external_cmd.py"), "w", encoding="utf-8") as f:
            f.write('__fp__ = {"name": "external_cmd", "type": "commands"}\n')
        # private 中不存在同名 → _find_asset 找到 fetched → promote 拒绝
        assert _run("promote", "external_cmd") == 1

    def test_share_requires_public(self, tmp_path):
        """share 前置：资产必须已在 public（先 promote）。"""
        repo = tmp_path / "share-repo"
        repo.mkdir()
        (repo / "fp.ext.json").write_text('{"schema": 1, "assets": {}}', encoding="utf-8")
        _run("new", "commands", "greet")
        # private 资产 → 拒绝
        assert _run("share", "greet", "--repo", str(repo)) == 1
        # promote 后 → 放行
        assert _run("promote", "greet") == 0
        assert _run("share", "greet", "--repo", str(repo)) == 0

    def test_share_requires_index(self, tmp_path):
        """share 前置：分享仓库必须已有 fp.ext.json，缺则拒绝（不自动创建）。"""
        repo = tmp_path / "share-repo"
        repo.mkdir()
        _run("new", "commands", "greet")
        assert _run("promote", "greet") == 0
        # 无 fp.ext.json → 拒绝
        assert _run("share", "greet", "--repo", str(repo)) == 1
        # 手动创建清单 → 放行，并登记资产
        (repo / "fp.ext.json").write_text(
            '{"schema": 1, "author": "tester", "license": "MIT", "assets": {}}', encoding="utf-8"
        )
        assert _run("share", "greet", "--repo", str(repo)) == 0
        assert (repo / "commands" / "greet.py").exists()
        import json

        idx = json.loads((repo / "fp.ext.json").read_text(encoding="utf-8"))
        assert "commands/greet" in idx["assets"]

    def test_share_requires_manifest(self, tmp_path):
        """share 前置：资产必须自描述（有 __fp__），缺失拒绝；补上后放行。"""
        repo = tmp_path / "share-repo"
        repo.mkdir()
        (repo / "fp.ext.json").write_text(
            '{"schema": 1, "author": "t", "license": "MIT", "assets": {}}', encoding="utf-8"
        )
        # 手写一个无 __fp__ 的命令（文件名 = 语义名）
        cdir = source_dir("private", "commands")
        os.makedirs(cdir, exist_ok=True)
        bare = os.path.join(cdir, "bare.py")
        with open(bare, "w", encoding="utf-8") as f:
            f.write('PLUGIN_DEFINITION = {"type": "function", "function": {"name": "bare"}}\n')
        assert _run("promote", "bare") == 0
        # 无 __fp__ → share 拒绝
        assert _run("share", "bare", "--repo", str(repo)) == 1
        # 补上 __fp__（模拟 init 结果）→ 放行
        with open(os.path.join(source_dir("public", "commands"), "bare.py"), "w", encoding="utf-8") as f:
            f.write('__fp__ = {"name": "bare", "version": "0.1.0", "description": "", "type": "commands"}\n')
        assert _run("share", "bare", "--repo", str(repo)) == 0

    def test_share_idempotent(self, tmp_path, capsys):
        """share 幂等：内容无变化再次 share → 跳过并提示已是最新。"""
        repo = tmp_path / "share-repo"
        repo.mkdir()
        (repo / "fp.ext.json").write_text(
            '{"schema": 1, "author": "t", "license": "MIT", "assets": {}}', encoding="utf-8"
        )
        _run("new", "commands", "greet")
        assert _run("promote", "greet") == 0
        # 第一次 share
        assert _run("share", "greet", "--repo", str(repo)) == 0
        # 第二次 share（内容无变化）→ 跳过
        assert _run("share", "greet", "--repo", str(repo)) == 0
        out = capsys.readouterr().out
        assert "已是最新" in out

    def test_share_schema_required(self, tmp_path):
        """share 前置：fp.ext.json 必须含 schema 字段（协议版本）。"""
        repo = tmp_path / "share-repo"
        repo.mkdir()
        # 无 schema 字段的清单
        (repo / "fp.ext.json").write_text('{"author": "t", "assets": {}}', encoding="utf-8")
        _run("new", "commands", "greet")
        assert _run("promote", "greet") == 0
        assert _run("share", "greet", "--repo", str(repo)) == 1

    def test_remove_goes_to_trash(self):
        _run("new", "tools", "hello")
        assert _run("remove", "hello") == 0
        # 文件从 private 消失
        assert not os.path.exists(source_dir("private", "tools") + "/hello_plugin.py")
        # 进入回收站
        trash = os.path.join(str(os.path.dirname(source_root("private"))), ".trash")
        assert os.path.isdir(trash)

    def test_check_clean(self, capsys):
        _run("new", "tools", "hello")
        assert _run("check") == 0
        out = capsys.readouterr().out
        assert "体检通过" in out

    def test_migrate_legacy(self):
        """老结构识别即迁移到 private。"""
        data = os.path.dirname(source_root("private"))
        legacy = os.path.join(data, "tools", "extensions")
        os.makedirs(legacy, exist_ok=True)
        with open(os.path.join(legacy, "legacy_plugin.py"), "w", encoding="utf-8") as f:
            f.write('__fp__ = {"name": "legacy", "type": "tools"}\n')
        assert _run("migrate") == 0
        # 已移动到 private/tools/extensions
        assert os.path.isfile(os.path.join(source_dir("private", "tools"), "legacy_plugin.py"))
        # 老目录不存在
        assert not os.path.isdir(legacy)
        # 幂等：再次迁移无操作
        assert _run("migrate") == 0

    def test_user_dirs_priority(self):
        """user_dirs 返回顺序 fetched → public → private（低→高优先级）。"""
        dirs = user_dirs("memory")
        assert dirs[-1].endswith("private/memory")
        assert dirs[0].endswith("fetched/memory")
        assert len(dirs) == 3

    def test_detect_legacy_empty(self):
        assert detect_legacy() == []
