"""测试 memory 插件 — 长期记忆读写

覆盖重点：
- memory_save：参数校验、root 校验、禁用分类、写文件格式、本地/全局目录
- memory_read 四入口：空参索引、name 精确读取（本地优先）、path 分类浏览、query 全文搜索
- frontmatter 解析、正文提取
"""

import os
from datetime import datetime

import pytest

import fp_core.tools.extensions.memory_read_plugin as mem_read
import fp_core.tools.extensions.memory_save_plugin as mem_save
from fp_core import config


@pytest.fixture
def memory_env(tmp_path, monkeypatch):
    """隔离记忆目录（三来源模型：private/public/fetched + 项目内）"""
    data_dir = tmp_path / "fpdata"
    monkeypatch.setattr(config, "_FP_DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "MEMORY_DIR", str(data_dir / "memory"))  # 兼容保留（旧引用）
    monkeypatch.setattr(config, "MEMORY_DIR_LOCAL", os.path.join(".fp", "memory"))
    # 隔离 cwd：避免读到真实项目的 .fp/memory（默认空，_set_local 可切换）
    monkeypatch.chdir(tmp_path)

    def _set_local(cwd: str):
        monkeypatch.chdir(cwd)

    # 全局记忆读写最高优先级来源 = private/memory（user_dirs("memory")[-1]）
    private_mem = os.path.join(str(data_dir), "private", "memory")
    return {"global": private_mem, "local": str(data_dir / "local_memory"), "set_local": _set_local}


@pytest.fixture
def local_project(memory_env, tmp_path):
    """创建本地项目目录（作为 cwd）"""
    proj = tmp_path / "project"
    proj.mkdir()
    return proj


def _write_mem(directory: str, name: str, type_: str, description: str, body: str):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, f"{name}.md"), "w", encoding="utf-8") as f:
        f.write("---\n")
        f.write(f"name: {name}\n")
        f.write(f"description: {description}\n")
        f.write(f"type: {type_}\n")
        f.write("created: 2024-01-01 10:00\n")
        f.write("---\n\n")
        f.write(body + "\n")


# ═══════════════════════════════════════════════════════════
# memory_save
# ═══════════════════════════════════════════════════════════


class TestMemorySave:
    @pytest.mark.asyncio
    async def test_save_success_global(self, memory_env):
        result = await mem_save.execute({
            "root": "~",
            "category": "skill",
            "name": "test_mem",
            "description": "desc",
            "content": "内容",
        })
        assert "✅" in result

        path = os.path.join(memory_env["global"], "test_mem.md")
        assert os.path.exists(path)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "name: test_mem" in content
        assert "description: desc" in content
        assert "type: skill" in content
        assert "内容" in content

    @pytest.mark.asyncio
    async def test_save_success_local(self, memory_env, local_project):
        memory_env["set_local"](str(local_project))
        result = await mem_save.execute({
            "root": ".",
            "category": "project",
            "name": "local_mem",
            "description": "本地",
            "content": "本地内容",
        })
        assert "本地" in result
        path = os.path.join(str(local_project), ".fp", "memory", "local_mem.md")
        assert os.path.exists(path)

    @pytest.mark.asyncio
    async def test_save_missing_params(self, memory_env):
        with pytest.raises(ValueError, match="需要以下参数"):
            await mem_save.execute({"root": "~", "category": "skill", "name": "x", "description": "y"})

    @pytest.mark.asyncio
    async def test_save_invalid_root(self, memory_env):
        with pytest.raises(ValueError, match="root 必须是"):
            await mem_save.execute({
                "root": "/",
                "category": "skill",
                "name": "x",
                "description": "y",
                "content": "z",
            })

    @pytest.mark.asyncio
    async def test_save_forbidden_category(self, memory_env):
        with pytest.raises(ValueError, match="被禁用"):
            await mem_save.execute({
                "root": "~",
                "category": "core",
                "name": "x",
                "description": "y",
                "content": "z",
            })

    @pytest.mark.asyncio
    async def test_save_sanitizes_name(self, memory_env):
        """空格/斜杠被替换为下划线"""
        await mem_save.execute({
            "root": "~",
            "category": "skill",
            "name": "my mem/1",
            "description": "d",
            "content": "c",
        })
        assert os.path.exists(os.path.join(memory_env["global"], "my_mem_1.md"))

    @pytest.mark.asyncio
    async def test_save_overwrites_existing(self, memory_env):
        _write_mem(memory_env["global"], "dup", "skill", "旧", "旧内容")
        await mem_save.execute({
            "root": "~",
            "category": "skill",
            "name": "dup",
            "description": "新",
            "content": "新内容",
        })
        with open(os.path.join(memory_env["global"], "dup.md"), encoding="utf-8") as f:
            content = f.read()
        assert "新内容" in content
        assert "旧内容" not in content

    @pytest.mark.asyncio
    async def test_save_writes_created_and_updated(self, memory_env):
        """新建文件应同时写 created 与 updated（freshness 追踪）"""
        await mem_save.execute({
            "root": "~",
            "category": "skill",
            "name": "fresh",
            "description": "d",
            "content": "c",
        })
        with open(os.path.join(memory_env["global"], "fresh.md"), encoding="utf-8") as f:
            content = f.read()
        assert "created: " in content
        assert "updated: " in content
        # 新建时 created == updated
        c = content.split("created: ")[1].split("\n")[0]
        u = content.split("updated: ")[1].split("\n")[0]
        assert c == u

    @pytest.mark.asyncio
    async def test_save_overwrite_keeps_created_refreshes_updated(self, memory_env):
        """覆盖更新应保留原 created、刷新 updated"""
        _write_mem(memory_env["global"], "dup2", "skill", "旧", "旧内容")
        await mem_save.execute({
            "root": "~",
            "category": "skill",
            "name": "dup2",
            "description": "新",
            "content": "新内容",
        })
        with open(os.path.join(memory_env["global"], "dup2.md"), encoding="utf-8") as f:
            content = f.read()
        assert "created: 2024-01-01 10:00" in content  # 保留原 created
        assert "updated: " in content
        u = content.split("updated: ")[1].split("\n")[0]
        assert u != "2024-01-01 10:00"

    @pytest.mark.asyncio
    async def test_save_with_hint(self, memory_env):
        """可选 hint（路径/命令等短硬事实）写入 frontmatter"""
        await mem_save.execute({
            "root": "~",
            "category": "user",
            "name": "with_hint",
            "description": "d",
            "content": "c",
            "hint": "/media/zpb/data/codes/AI/agent",
        })
        with open(os.path.join(memory_env["global"], "with_hint.md"), encoding="utf-8") as f:
            content = f.read()
        assert "hint: /media/zpb/data/codes/AI/agent" in content


# ═══════════════════════════════════════════════════════════
# memory_read — 工具函数
# ═══════════════════════════════════════════════════════════


class TestMemoryReadHelpers:
    def test_parse_frontmatter(self):
        content = "---\nname: x\ndescription: d\ntype: skill\n---\n正文"
        fm = mem_read._parse_frontmatter(content)
        assert fm["name"] == "x"
        assert fm["type"] == "skill"

    def test_parse_frontmatter_fallback_line_scan(self):
        """yaml 解析失败时降级行扫描"""
        # 用非法 yaml 内容触发降级（构造一个 yaml 无法解析的 frontmatter）
        fm = mem_read._parse_frontmatter("---\nname: x\n  bad: [unclosed\n---\n")
        assert fm.get("name") == "x"

    def test_parse_frontmatter_no_frontmatter(self):
        assert mem_read._parse_frontmatter("纯文本") == {}

    def test_parse_memory_body(self):
        content = "---\nname: x\n---\n\n正文第一行\n正文第二行\n"
        assert mem_read._parse_memory_body(content) == "正文第一行\n正文第二行"

    def test_parse_memory_body_no_frontmatter(self):
        assert mem_read._parse_memory_body("纯正文") == "纯正文"

    def test_list_memories(self, memory_env):
        _write_mem(memory_env["global"], "a", "skill", "descA", "bodyA")
        memories = mem_read._list_memories(memory_env["global"])
        assert len(memories) == 1
        assert memories[0]["name"] == "a"
        assert memories[0]["type"] == "skill"
        assert memories[0]["description"] == "descA"

    def test_list_memories_ignores_non_md(self, memory_env):
        os.makedirs(memory_env["global"], exist_ok=True)
        with open(os.path.join(memory_env["global"], "notes.txt"), "w") as f:
            f.write("x")
        assert mem_read._list_memories(memory_env["global"]) == []

    def test_list_memories_missing_dir(self):
        assert mem_read._list_memories("/nonexistent/dir") == []

    def test_build_tree_index(self, memory_env):
        _write_mem(memory_env["global"], "alpha", "skill", "d", "b")
        global_mems = mem_read._list_memories(memory_env["global"])
        idx = mem_read._build_tree_index(global_mems, [])
        assert "## 我的长期记忆索引" in idx
        assert "~/（全局，1条）" in idx
        assert "skill:" in idx
        assert "alpha" in idx

    def test_build_tree_index_with_local(self, memory_env, local_project):
        local_mem_dir = os.path.join(str(local_project), ".fp", "memory")
        _write_mem(local_mem_dir, "local_mem", "project", "本地", "b")
        local_mems = mem_read._list_memories(local_mem_dir)
        idx = mem_read._build_tree_index([], local_mems)
        assert "./（本地 📍 .fp/memory，1条）" in idx

    def test_build_category_browse(self):
        mems = [{"name": "n1", "description": "d1", "type": "skill"}]
        out = mem_read._build_category_browse("skill", mems, "~")
        assert "~/skill/（共 1 条）" in out
        assert "n1" in out

    def test_entry_label_no_hint(self):
        assert mem_read._entry_label({"name": "n1", "hint": ""}) == "n1"

    def test_entry_label_with_hint(self):
        m = {"name": "my_code_location", "hint": "/media/zpb/data/codes/AI/agent"}
        assert mem_read._entry_label(m) == "my_code_location→/media/zpb/data/codes/AI/agent"

    def test_entry_label_hint_truncated(self):
        m = {"name": "x", "hint": "a" * 100}
        assert len(mem_read._entry_label(m)) < 60
        assert "…" in mem_read._entry_label(m)

    def test_stale_note_over_threshold(self):
        note = mem_read._stale_note("2024-01-01 10:00")
        assert note.startswith("⚠️ 记忆已")

    def test_stale_note_fresh(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        assert mem_read._stale_note(now) == ""

    def test_stale_note_bad_format(self):
        assert mem_read._stale_note("not-a-date") == ""

    def test_build_tree_index_shows_hint(self, memory_env):
        """索引行应内联 hint 硬事实，免一次 memory_read"""
        mems = [
            {
                "name": "loc",
                "type": "user",
                "description": "d",
                "hint": "/path/to/x",
                "created": "2026-01-01 10:00",
                "updated": "2026-01-01 10:00",
                "content": "---\n---\n",
                "root": "~",
            }
        ]
        idx = mem_read._build_tree_index(mems, [])
        assert "loc→/path/to/x" in idx


# ═══════════════════════════════════════════════════════════
# memory_read — execute 四入口
# ═══════════════════════════════════════════════════════════


class TestMemoryReadExecute:
    @pytest.mark.asyncio
    async def test_empty_args_returns_index(self, memory_env):
        _write_mem(memory_env["global"], "a", "skill", "d", "b")
        result = await mem_read.execute({})
        assert "我的长期记忆索引" in result
        assert "skill:" in result

    @pytest.mark.asyncio
    async def test_read_by_name(self, memory_env):
        _write_mem(memory_env["global"], "proj", "project", "项目记忆", "核心内容")
        result = await mem_read.execute({"name": "proj"})
        assert "📋" in result
        assert "项目记忆" in result
        assert "核心内容" in result

    @pytest.mark.asyncio
    async def test_read_by_name_not_found_with_hints(self, memory_env):
        _write_mem(memory_env["global"], "qwen_cookie", "skill", "d", "b")
        result = await mem_read.execute({"name": "qwen"})
        assert "未找到" in result
        assert "qwen_cookie" in result  # fuzzy 提示

    @pytest.mark.asyncio
    async def test_read_name_local_preferred(self, memory_env, local_project):
        """同名冲突时本地优先"""
        _write_mem(memory_env["global"], "dup", "global_type", "全局版", "全局内容")
        local_mem_dir = os.path.join(str(local_project), ".fp", "memory")
        _write_mem(local_mem_dir, "dup", "local_type", "本地版", "本地内容")

        memory_env["set_local"](str(local_project))
        result = await mem_read.execute({"name": "dup"})

        assert "本地版" in result
        assert "本地内容" in result
        assert "全局内容" not in result

    @pytest.mark.asyncio
    async def test_read_by_path_global(self, memory_env):
        _write_mem(memory_env["global"], "a", "skill", "技能", "b")
        result = await mem_read.execute({"path": "~/skill"})
        assert "~/skill/（共 1 条）" in result

    @pytest.mark.asyncio
    async def test_read_by_path_empty_category(self, memory_env):
        _write_mem(memory_env["global"], "a", "skill", "d", "b")
        result = await mem_read.execute({"path": "~/project"})
        assert "该分类下无记忆" in result
        assert "skill" in result  # 提示可选分类

    @pytest.mark.asyncio
    async def test_read_by_path_bad_format(self, memory_env):
        result = await mem_read.execute({"path": "badpath"})
        assert "path 格式错误" in result

    @pytest.mark.asyncio
    async def test_read_by_path_local(self, memory_env, local_project):
        local_mem_dir = os.path.join(str(local_project), ".fp", "memory")
        _write_mem(local_mem_dir, "a", "project", "本地项目", "b")
        memory_env["set_local"](str(local_project))
        result = await mem_read.execute({"path": "./project"})
        assert "./project/（共 1 条）" in result

    @pytest.mark.asyncio
    async def test_read_by_query_single_keyword(self, memory_env):
        _write_mem(memory_env["global"], "a", "skill", "关于 cookie 的", "b")
        _write_mem(memory_env["global"], "b", "skill", "无关的", "b")
        result = await mem_read.execute({"query": "cookie"})
        assert "共 1 条" in result
        assert "a" in result
        assert "b — 无关的" not in result

    @pytest.mark.asyncio
    async def test_read_by_query_multiple_keywords_and(self, tmp_path, monkeypatch):
        """多关键词 AND 匹配（隔离三来源环境确保空库）"""
        monkeypatch.setattr(config, "_FP_DATA_DIR", str(tmp_path))
        result = await mem_read.execute({"query": "foo bar"})
        assert "无匹配" in result

    @pytest.mark.asyncio
    async def test_read_by_query_searches_body(self, memory_env):
        _write_mem(memory_env["global"], "a", "skill", "标题", "正文里有独特关键词xyz")
        result = await mem_read.execute({"query": "xyz"})
        assert "共 1 条" in result

    @pytest.mark.asyncio
    async def test_read_by_query_no_match(self, memory_env):
        result = await mem_read.execute({"query": "不存在关键词"})
        assert "无匹配" in result
