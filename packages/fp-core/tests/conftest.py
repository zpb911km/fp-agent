"""pytest fixtures — 隔离文件 I/O，避免污染真实配置"""

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """每个测试前重置 config 模块的 JSON 缓存，防止跨测试污染。

    config 模块在 import 时执行 _load_json_config() 并缓存到 _json_cfg。
    测试中修改环境变量或配置文件后，需要手动重置缓存。
    """
    import fp_core.config as cfg

    cfg._json_cfg = {}
    yield


@pytest.fixture
def temp_sessions_dir() -> Generator[str, None, None]:
    """创建临时会话目录，覆盖 config.SESSIONS_DIR。

    使用方式:
        import fp_core.config as cfg
        cfg.SESSIONS_DIR = temp_sessions_dir
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def temp_skills_dir() -> Generator[str, None, None]:
    """创建临时技能目录，可用于测试 SkillLoader。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def sample_skill_file(temp_skills_dir: str) -> str:
    """在临时技能目录中创建一个标准 YAML frontmatter 技能文件。"""
    content = """---
name: test_skill
title: 测试技能
description: 用于单元测试的技能
category: testing
version: "2.0"
priority: 10
---

执行 `echo "hello from test_skill"`
"""
    path = Path(temp_skills_dir) / "test_skill.md"
    path.write_text(content, encoding="utf-8")
    return str(path)


@pytest.fixture
def sample_skill_no_frontmatter(temp_skills_dir: str) -> str:
    """创建无 YAML frontmatter 的技能文件。"""
    content = "直接输出 'hello world'"
    path = Path(temp_skills_dir) / "simple_skill.md"
    path.write_text(content, encoding="utf-8")
    return str(path)
