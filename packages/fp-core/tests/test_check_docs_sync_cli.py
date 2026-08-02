"""scripts/check_docs_sync.py CLI — 「代码变更→文档跟进」机制的保护测试

防止「防漂移机制」自身腐烂：脚本必须能正确判定
「代码变了、关联文档没变」→ 提醒/阻断 的核心场景，
以及「内容修改不误报清单文档、结构变更才提醒清单文档」的精细化行为。

每个场景都在独立的临时 git 仓库中验证（真实 diff，不 mock）。
"""

import fnmatch
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check_docs_sync.py"

# 脚本内置规则中，命令文件内容修改 → 命令系统文档；结构变更额外 → 命令参考/README
COMMAND_FILE = "packages/fp-core/src/fp_core/commands/echo.py"


def _load_script_module():
    """从源码加载脚本模块（无副作用），用于读取 DOC_RULES"""
    spec = importlib.util.spec_from_file_location("check_docs_sync", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本模块: {SCRIPT}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _affected_docs_for(path: str) -> list[str]:
    """复现脚本规则：给定代码文件，内容修改（M）时受影响的文档（docs_any，first-match）"""
    mod = _load_script_module()
    for glob, docs_any, _docs_structural in mod.DOC_RULES:
        if fnmatch.fnmatch(path, glob):
            return list(docs_any)
    return []


def _init_repo(root: Path) -> None:
    """建一个最小 git 仓库：1 个命令文件 + 1 个命令文档 + README"""
    (root / COMMAND_FILE).parent.mkdir(parents=True)
    (root / COMMAND_FILE).write_text("'''echo command'''\n", encoding="utf-8")
    (root / "docs" / "guide").mkdir(parents=True)
    (root / "docs" / "guide" / "命令参考.md").write_text("# 命令参考\n", encoding="utf-8")
    (root / "README.md").write_text("# FP\n", encoding="utf-8")

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    for c in [
        ["git", "-C", str(root), "init", "-q"],
        ["git", "-C", str(root), "add", "-A"],
        ["git", "-C", str(root), "commit", "-q", "-m", "init"],
    ]:
        subprocess.run(c, capture_output=True, text=True, env=env, check=True)


def _run_script(root: Path, *extra: str, allow: bool = False) -> subprocess.CompletedProcess:
    env = {**os.environ}
    if allow:
        env["FP_DOCS_SYNC_ALLOW"] = "1"
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(root), *extra],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _modify(path: Path, content: str, root: Path) -> None:
    """改文件并 git add（模拟提交前暂存）"""
    path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", str(path.relative_to(root))], capture_output=True, check=True)


def test_no_code_change_passes(tmp_path):
    """只改文档（无代码变更）→ exit 0"""
    root = tmp_path / "repo"
    _init_repo(root)
    _modify(root / "README.md", "# FP v2\n", root)

    r = _run_script(root)
    assert r.returncode == 0, r.stdout + r.stderr


def test_code_change_doc_not_updated_blocks(tmp_path):
    """代码变了 + 关联文档没变 → exit 1，且提醒里点名受影响文档"""
    root = tmp_path / "repo"
    _init_repo(root)
    _modify(root / COMMAND_FILE, "'''echo command v2'''\n", root)

    r = _run_script(root)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "命令系统" in r.stdout


def test_code_change_doc_updated_passes(tmp_path):
    """代码变了 + 规则关联的全部文档也同步变了 → exit 0"""
    root = tmp_path / "repo"
    _init_repo(root)
    _modify(root / COMMAND_FILE, "'''echo command v2'''\n", root)
    for doc in _affected_docs_for(COMMAND_FILE):
        p = root / doc
        p.parent.mkdir(parents=True, exist_ok=True)
        _modify(p, f"updated: {doc}\n", root)

    r = _run_script(root)
    assert r.returncode == 0, r.stdout + r.stderr


def test_allow_env_overrides_block(tmp_path):
    """FP_DOCS_SYNC_ALLOW=1 → 代码变了文档没变也放行（exit 0，仅提醒）"""
    root = tmp_path / "repo"
    _init_repo(root)
    _modify(root / COMMAND_FILE, "'''echo command v2'''\n", root)

    r = _run_script(root, allow=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "命令系统" in r.stdout  # 提醒仍然输出


def test_modify_does_not_remind_manifest(tmp_path):
    """内容修改（M）→ 只提醒 dev 行为文档，不误报 README/命令参考（精细化核心）"""
    root = tmp_path / "repo"
    _init_repo(root)
    _modify(root / COMMAND_FILE, "'''echo command v2'''\n", root)

    r = _run_script(root)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "命令系统" in r.stdout
    assert "命令参考" not in r.stdout
    assert "README.md" not in r.stdout


def test_structural_change_reminds_manifest(tmp_path):
    """新增命令文件（A，结构变更）→ 额外提醒清单类文档（命令参考/README）"""
    root = tmp_path / "repo"
    _init_repo(root)
    p = root / "packages/fp-core/src/fp_core/commands/uniq.py"
    p.write_text("'''uniq command'''\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(root), "add", "packages/fp-core/src/fp_core/commands/uniq.py"],
        capture_output=True,
        check=True,
    )

    r = _run_script(root)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "命令参考" in r.stdout
    assert "README.md" in r.stdout


def test_unmapped_change_warns_but_passes(tmp_path):
    """未被规则覆盖的代码路径变更 → 输出提醒（不静默），但不阻断（exit 0）"""
    root = tmp_path / "repo"
    _init_repo(root)
    p = root / "top_level_script.py"
    p.write_text("print(1)\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "top_level_script.py"], capture_output=True, check=True)

    r = _run_script(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "未被任何文档规则覆盖" in r.stdout  # 提醒不静默


def test_audit_lists_unmapped(tmp_path):
    """--audit：列出未被规则覆盖的文件（跳过 build/ 等产物），exit 1"""
    root = tmp_path / "repo"
    _init_repo(root)
    # 不被覆盖的文件
    (root / "top_level_script.py").write_text("print(1)\n", encoding="utf-8")
    # 构建产物（应被 audit 忽略）
    (root / "packages" / "fp-terminal" / "build" / "lib" / "fp_cli").mkdir(parents=True)
    (root / "packages" / "fp-terminal" / "build" / "lib" / "fp_cli" / "x.py").write_text("print(1)\n", encoding="utf-8")
    # 测试文件（应被 audit 忽略）
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_x.py").write_text("def test(): pass\n", encoding="utf-8")

    r = _run_script(root, "--audit")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "top_level_script.py" in r.stdout
    assert "build" not in r.stdout  # 产物不报
    assert "test_x.py" not in r.stdout  # 测试不报


def test_audit_passes_when_all_mapped(tmp_path):
    """--audit：所有代码文件都被规则覆盖 → exit 0"""
    root = tmp_path / "repo"
    _init_repo(root)

    r = _run_script(root, "--audit")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "无盲区" in r.stdout


def test_since_mode(tmp_path):
    """--since HEAD~1：检查最近一次提交（含已提交的代码变更）→ 能发现文档没同步"""
    root = tmp_path / "repo"
    _init_repo(root)

    # 提交一个「代码变了但文档没变」的 commit
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    (root / COMMAND_FILE).write_text("'''echo command v2'''\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", COMMAND_FILE], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "change cmd"], env=env, check=True)

    r = _run_script(root, "--since", "HEAD~1")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "命令系统" in r.stdout


def test_pre_commit_hook_wired():
    """pre-commit 配置里必须存在 docs-sync 钩子（防止有人删掉门禁）"""
    cfg = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "docs-sync" in cfg
    assert "scripts/check_docs_sync.py" in cfg
    # 钩子的 files 正则必须覆盖测试目录（否则测试变更不会触发门禁）
    assert "packages/fp-core/tests/" in cfg


def test_test_file_change_reminds_test_doc(tmp_path):
    """测试文件变更 → 提醒 docs/dev/测试.md（测试也纳入文档同步门禁）"""
    root = tmp_path / "repo"
    _init_repo(root)
    p = root / "packages/fp-core/tests/test_xyz.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("def test_xyz(): pass\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(root), "add", "packages/fp-core/tests/test_xyz.py"],
        capture_output=True,
        check=True,
    )

    r = _run_script(root)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "测试.md" in r.stdout
