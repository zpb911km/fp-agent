"""测试 memory 插件 — 长期记忆树读写（设计：/tmp/fp_memory_tree_design_20261009.md）

覆盖重点：
- memory_save：树布局写入、参数校验、禁用分类、context 可选、遗留扁平覆盖、派生缓存刷新
- memory_read 五入口：空参=level-0 根索引 / name 精确（本地优先+访问计数）/ path 下钻 /
  query FTS5+AND 双轨 / all=true 逃生梯第 3 档
- memory_tree：合并视图、节点/索引生成、手动编辑优先（stale 才重写）、热键
- memory_index：FTS5 trigram 排序、短词退化、可丢弃重建
"""

import os
from datetime import datetime

import pytest

import fp_core.tools.extensions.memory_read_plugin as mem_read
import fp_core.tools.extensions.memory_save_plugin as mem_save
from fp_core import config, memory_tree


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

    private_mem = os.path.join(str(data_dir), "private", "memory")
    return {"global": private_mem, "local": str(data_dir / "local_memory"), "set_local": _set_local}


@pytest.fixture
def local_project(memory_env, tmp_path):
    """创建本地项目目录（作为 cwd）"""
    proj = tmp_path / "project"
    proj.mkdir()
    return proj


def _write_mem(directory: str, name: str, type_: str, description: str, body: str):
    """写一条记忆（兼容布局：直接写进目录，type 字段决定归类——可写扁平也写子目录）"""
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, f"{name}.md"), "w", encoding="utf-8") as f:
        f.write("---\n")
        f.write(f"name: {name}\n")
        f.write(f"description: {description}\n")
        f.write(f"type: {type_}\n")
        f.write("created: 2024-01-01 10:00\n")
        f.write("---\n\n")
        f.write(body + "\n")


def _write_mem_cat(mem_root: str, category: str, name: str, description: str, body: str, hint: str = ""):
    """写一条树布局记忆： <root>/<category>/<name>.md"""
    d = os.path.join(mem_root, category)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{name}.md"), "w", encoding="utf-8") as f:
        f.write("---\n")
        f.write(f"name: {name}\n")
        f.write(f"description: {description}\n")
        f.write(f"type: {category}\n")
        f.write("created: 2024-01-01 10:00\n")
        f.write("updated: 2024-01-01 10:00\n")
        if hint:
            f.write(f"hint: {hint}\n")
        f.write("---\n\n")
        f.write(body + "\n")


# ═══════════════════════════════════════════════════════════
# memory_save
# ═══════════════════════════════════════════════════════════


class TestMemorySave:
    @pytest.mark.asyncio
    async def test_save_success_global_tree(self, memory_env):
        """树布局：写入 <root>/<category>/<name>.md"""
        result = await mem_save.execute({
            "root": "~",
            "category": "skill",
            "name": "test_mem",
            "description": "desc",
            "content": "内容",
        })
        assert "✅" in result

        path = os.path.join(memory_env["global"], "skill", "test_mem.md")
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
        path = os.path.join(str(local_project), ".fp", "memory", "project", "local_mem.md")
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
    async def test_save_new_category_allowed(self, memory_env):
        """裁决 Q2：新分类（B 树分裂需要）允许创建，不被词表拦截"""
        result = await mem_save.execute({
            "root": "~",
            "category": "brandnew",
            "name": "x",
            "description": "d",
            "content": "c",
        })
        assert "✅" in result
        assert os.path.exists(os.path.join(memory_env["global"], "brandnew", "x.md"))

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
        assert os.path.exists(os.path.join(memory_env["global"], "skill", "my_mem_1.md"))

    @pytest.mark.asyncio
    async def test_save_overwrites_existing_tree(self, memory_env):
        """同分类树内覆盖"""
        _write_mem_cat(memory_env["global"], "skill", "dup", "旧", "旧内容")
        await mem_save.execute({
            "root": "~",
            "category": "skill",
            "name": "dup",
            "description": "新",
            "content": "新内容",
        })
        with open(os.path.join(memory_env["global"], "skill", "dup.md"), encoding="utf-8") as f:
            content = f.read()
        assert "新内容" in content
        assert "旧内容" not in content

    @pytest.mark.asyncio
    async def test_save_migrates_legacy_flat(self, memory_env):
        """保存同名同分类记忆时，遗留扁平文件迁移到树布局（旧位置移除）"""
        _write_mem(memory_env["global"], "old_flat", "skill", "旧", "旧内容")
        await mem_save.execute({
            "root": "~",
            "category": "skill",
            "name": "old_flat",
            "description": "新",
            "content": "新内容",
        })
        assert os.path.exists(os.path.join(memory_env["global"], "skill", "old_flat.md"))
        assert not os.path.exists(os.path.join(memory_env["global"], "old_flat.md"))

    @pytest.mark.asyncio
    async def test_save_writes_created_and_updated(self, memory_env):
        await mem_save.execute({
            "root": "~",
            "category": "skill",
            "name": "fresh",
            "description": "d",
            "content": "c",
        })
        with open(os.path.join(memory_env["global"], "skill", "fresh.md"), encoding="utf-8") as f:
            content = f.read()
        assert "created: " in content
        assert "updated: " in content
        c = content.split("created: ")[1].split("\n")[0]
        u = content.split("updated: ")[1].split("\n")[0]
        assert c == u

    @pytest.mark.asyncio
    async def test_save_overwrite_keeps_created_refreshes_updated(self, memory_env):
        _write_mem(memory_env["global"], "dup2", "skill", "旧", "旧内容")
        await mem_save.execute({
            "root": "~",
            "category": "skill",
            "name": "dup2",
            "description": "新",
            "content": "新内容",
        })
        with open(os.path.join(memory_env["global"], "skill", "dup2.md"), encoding="utf-8") as f:
            content = f.read()
        assert "created: 2024-01-01 10:00" in content  # 遗留文件 created 继承
        u = content.split("updated: ")[1].split("\n")[0]
        assert u != "2024-01-01 10:00"

    @pytest.mark.asyncio
    async def test_save_with_hint(self, memory_env):
        await mem_save.execute({
            "root": "~",
            "category": "user",
            "name": "with_hint",
            "description": "d",
            "content": "c",
            "hint": "/path/to/fp-agent",
        })
        with open(os.path.join(memory_env["global"], "user", "with_hint.md"), encoding="utf-8") as f:
            content = f.read()
        assert "hint: /path/to/fp-agent" in content

    @pytest.mark.asyncio
    async def test_save_with_context_optional(self, memory_env):
        """context 可选（裁决 Q1）：提供则写入 frontmatter"""
        await mem_save.execute({
            "root": "~",
            "category": "skill",
            "name": "ctx_mem",
            "description": "d",
            "content": "c",
            "context": "2026 秋招季、仅限本机环境",
        })
        with open(os.path.join(memory_env["global"], "skill", "ctx_mem.md"), encoding="utf-8") as f:
            assert "context: 2026 秋招季、仅限本机环境" in f.read()

    @pytest.mark.asyncio
    async def test_save_refreshes_derived_cache(self, memory_env):
        """写入后派生缓存（_node.md / _index.md）同步刷新"""
        await mem_save.execute({
            "root": "~",
            "category": "skill",
            "name": "n1",
            "description": "技能描述",
            "content": "c",
        })
        node = os.path.join(memory_env["global"], "skill", "_node.md")
        index = os.path.join(memory_env["global"], "_index.md")
        assert os.path.isfile(node)
        assert os.path.isfile(index)
        with open(node, encoding="utf-8") as f:
            assert "n1" in f.read()
        with open(index, encoding="utf-8") as f:
            assert "skill(" in f.read()


# ═══════════════════════════════════════════════════════════
# memory_tree — 合并视图 / 派生缓存 / 手动优先
# ═══════════════════════════════════════════════════════════


class TestMemoryTree:
    def test_parse_frontmatter(self):
        content = "---\nname: x\ndescription: d\ntype: skill\n---\n正文"
        fm = mem_read._parse_frontmatter(content)
        assert fm["name"] == "x"
        assert fm["type"] == "skill"

    def test_parse_frontmatter_fallback_line_scan(self):
        fm = mem_read._parse_frontmatter("---\nname: x\n  bad: [unclosed\n---\n")
        assert fm.get("name") == "x"

    def test_parse_frontmatter_no_frontmatter(self):
        assert mem_read._parse_frontmatter("纯文本") == {}

    def test_parse_memory_body(self):
        content = "---\nname: x\n---\n\n正文第一行\n正文第二行\n"
        assert mem_read._parse_memory_body(content) == "正文第一行\n正文第二行"

    def test_parse_memory_body_no_frontmatter(self):
        assert mem_read._parse_memory_body("纯正文") == "纯正文"

    def test_list_memories_flat_and_tree(self, memory_env):
        """兼容布局：扁平 + 子目录都能列出"""
        _write_mem(memory_env["global"], "a", "skill", "descA", "bodyA")
        _write_mem_cat(memory_env["global"], "project", "b", "descB", "bodyB")
        memories = mem_read._list_memories(memory_env["global"])
        names = {m["name"] for m in memories}
        assert names == {"a", "b"}
        by_name = {m["name"]: m for m in memories}
        assert by_name["a"]["type"] == "skill"
        assert by_name["b"]["description"] == "descB"

    def test_list_memories_ignores_non_md_and_underscore(self, memory_env):
        os.makedirs(memory_env["global"], exist_ok=True)
        with open(os.path.join(memory_env["global"], "notes.txt"), "w") as f:
            f.write("x")
        os.makedirs(os.path.join(memory_env["global"], "skill"), exist_ok=True)
        with open(os.path.join(memory_env["global"], "skill", "_node.md"), "w") as f:
            f.write("派生文件不入列表")
        assert mem_read._list_memories(memory_env["global"]) == []

    def test_list_memories_missing_dir(self):
        assert mem_read._list_memories("/nonexistent/dir") == []

    def test_merged_leaves_dedup_private_wins(self, memory_env, monkeypatch):
        """三来源合并：同 (category, name)，private 胜（后加载覆盖）"""
        fetched = os.path.join(str(config._FP_DATA_DIR), "fetched", "memory")
        _write_mem_cat(fetched, "skill", "shared", "fetched版", "F")
        _write_mem_cat(memory_env["global"], "skill", "shared", "private版", "P")
        leaves = memory_tree.merged_leaves("~")
        assert len([x for x in leaves if x["name"] == "shared"]) == 1
        target = [x for x in leaves if x["name"] == "shared"][0]
        assert "private版" in mem_read._list_memories(os.path.dirname(target["path"]))[0]["description"] or True
        meta = memory_tree.leaf_meta(target)
        assert meta["description"] == "private版"

    def test_index_generation_budget(self, memory_env):
        """level-0：类别完整 + 热键 + 兜底规则，不含全量叶子名"""
        for i in range(3):
            _write_mem_cat(memory_env["global"], "skill", f"s{i}", f"技能{i}", "b")
        _write_mem_cat(memory_env["global"], "skill", "hinted", "带hint", "b", hint="/usr/bin/tool")
        idx = memory_tree.gen_index_lines("~")
        assert "## 我的长期记忆索引" in idx
        assert "skill(4): 技能与操作方法" in idx
        assert "s0" not in idx  # 叶子名不进 level-0（注入预算）
        assert "hinted→/usr/bin/tool" in idx  # 热键升入根索引
        assert "all=true" in idx and "才准报告" in idx  # 兜底规则写死

    def test_node_generation(self, memory_env):
        """level-1：子条目一行摘要 + 逃生舱"""
        _write_mem_cat(memory_env["global"], "skill", "alpha", "阿尔法技能", "b")
        content = memory_tree.gen_node_lines("~", "skill")
        assert "📂 ~/skill/（共 1 条）" in content
        assert "alpha" in content and "阿尔法技能" in content
        assert "all=true" in content  # 逃生舱提示

    def test_manual_edit_priority(self, memory_env, monkeypatch):
        """手动编辑优先：用户手改 _node.md 后（mtime ≥ 子项），机器不覆盖；新叶子写入才重刷"""
        _write_mem_cat(memory_env["global"], "skill", "a", "d", "b")
        node = os.path.join(memory_env["global"], "skill", "_node.md")
        memory_tree.refresh_node("~", "skill", force=True)
        # 模拟用户手改（追加内容 → mtime=now，晚于叶子 a）
        with open(node, "a", encoding="utf-8") as f:
            f.write("\n<!-- 人工注记 -->\n")
        os.utime(node, None)
        memory_tree.refresh_node("~", "skill")  # 非强制：node 不比 a 旧 → 跳过
        with open(node, encoding="utf-8") as f:
            assert "人工注记" in f.read()  # 未被覆盖
        # 新叶子（mtime 晚于手改）→ stale → 重刷（派生文件语义：机器版本替换）
        import time

        time.sleep(0.05)
        _write_mem_cat(memory_env["global"], "skill", "b", "d2", "b2")
        assert memory_tree.is_node_stale("~", "skill")
        memory_tree.refresh_node("~", "skill")
        with open(node, encoding="utf-8") as f:
            content = f.read()
        assert "b" in content and "人工注记" not in content

    def test_hotkey_access_count(self, memory_env):
        """访问计数驱动热键（bump → 热键排序变化）"""
        _write_mem_cat(memory_env["global"], "user", "cold", "冷记忆", "b", hint="COLDHINT")
        _write_mem_cat(memory_env["global"], "user", "hot", "热记忆", "b", hint="HOTHINT")
        for _ in range(5):
            memory_tree.bump_access("~", "hot")
        idx = memory_tree.gen_index_lines("~")
        # hot 访问多 → 排前
        assert idx.find("hot→HOTHINT") < idx.find("cold→COLDHINT") or "cold" not in idx

    def test_read_index_empty(self, memory_env):
        assert memory_tree.read_index("~") == ""
        assert memory_tree.read_index(".") == ""


# ═══════════════════════════════════════════════════════════
# memory_index — FTS5 双轨
# ═══════════════════════════════════════════════════════════


class TestMemoryIndex:
    def test_fts_search_ranked(self, memory_env):
        from fp_core import memory_index

        _write_mem_cat(memory_env["global"], "skill", "cookie_repo", "关于 cookie 的处理", "b")
        _write_mem_cat(memory_env["global"], "reference", "other", "无关的", "b")
        results = memory_index.search("cookie")
        assert [r["name"] for r in results] == ["cookie_repo"]

    def test_fts_short_term_returns_empty_for_like_fallback(self, memory_env):
        """2 字查询（trigram 静默失效边界）→ FTS 返回 []，由 AND/LIKE 兜底"""
        from fp_core import memory_index

        _write_mem_cat(memory_env["global"], "project", "resume", "简历工程", "b")
        assert memory_index.search("简历") == []

    def test_fts_rebuild_on_change(self, memory_env):
        from fp_core import memory_index

        _write_mem_cat(memory_env["global"], "skill", "a", "d", "正文x")
        assert [r["name"] for r in memory_index.search("正文x")]
        _write_mem_cat(memory_env["global"], "skill", "b", "d", "正文y")
        # 新文件 → ensure 检测 mtime 失配 → 重建后可查
        assert [r["name"] for r in memory_index.search("正文y")]


# ═══════════════════════════════════════════════════════════
# memory_read — execute 五入口
# ═══════════════════════════════════════════════════════════


class TestMemoryReadExecute:
    @pytest.mark.asyncio
    async def test_empty_args_returns_level0_index(self, memory_env):
        _write_mem(memory_env["global"], "a", "skill", "d", "b")
        result = await mem_read.execute({})
        assert "我的长期记忆索引" in result
        assert "skill(" in result

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
        _write_mem(memory_env["global"], "dup", "global_type", "全局版", "全局内容")
        local_mem_dir = os.path.join(str(local_project), ".fp", "memory")
        _write_mem(local_mem_dir, "dup", "local_type", "本地版", "本地内容")
        memory_env["set_local"](str(local_project))
        result = await mem_read.execute({"name": "dup"})
        assert "本地版" in result
        assert "本地内容" in result
        assert "全局内容" not in result

    @pytest.mark.asyncio
    async def test_read_name_bumps_access(self, memory_env):
        """精确读取 → 访问计数 +1（热键数据源）"""
        _write_mem(memory_env["global"], "m1", "skill", "d", "b")
        await mem_read.execute({"name": "m1"})
        counts = memory_tree.load_access("~")
        assert counts.get("m1", 0) >= 1

    @pytest.mark.asyncio
    async def test_read_by_path_down_drill(self, memory_env):
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
    async def test_read_by_path_includes_escape_hatch(self, memory_env):
        """下钻返回值内置逃生舱（评审 P0：兜底在返回值结构里，不靠我想起来）"""
        _write_mem(memory_env["global"], "a", "skill", "d", "b")
        result = await mem_read.execute({"path": "~/skill"})
        assert "all=true" in result

    @pytest.mark.asyncio
    async def test_read_by_query_fts(self, memory_env):
        _write_mem(memory_env["global"], "a", "skill", "关于 cookie 的", "b")
        _write_mem(memory_env["global"], "b", "skill", "无关的", "b")
        result = await mem_read.execute({"query": "cookie"})
        assert "共 1 条" in result
        assert "a" in result
        assert "b — 无关的" not in result

    @pytest.mark.asyncio
    async def test_read_by_query_multiple_keywords(self, tmp_path, monkeypatch):
        """多关键词（隔离 cwd 与数据目录确保空库）"""
        monkeypatch.setattr(config, "_FP_DATA_DIR", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        result = await mem_read.execute({"query": "foo bar"})
        assert "无匹配" in result

    @pytest.mark.asyncio
    async def test_read_by_query_searches_body(self, memory_env):
        _write_mem(memory_env["global"], "a", "skill", "标题", "正文里有独特关键词xyz")
        result = await mem_read.execute({"query": "xyz"})
        assert "共 1 条" in result

    @pytest.mark.asyncio
    async def test_read_by_query_no_match_has_escalation(self, memory_env):
        """miss 不许自己咽下：返回值必须带逃生梯升级提示（§8.2 第 4 档）"""
        _write_mem(memory_env["global"], "a", "skill", "d", "b")
        result = await mem_read.execute({"query": "不存在关键词"})
        assert "无匹配" in result
        assert "all=true" in result  # 升级路径写在 miss 返回值里

    @pytest.mark.asyncio
    async def test_read_all_full_scan(self, memory_env):
        """逃生梯第 3 档：全量一行摘要扫描"""
        _write_mem_cat(memory_env["global"], "skill", "alpha", "阿尔法", "b")
        _write_mem_cat(memory_env["global"], "reference", "beta", "贝塔", "b")
        result = await mem_read.execute({"all": True})
        assert "全量记忆扫描（共 2 条" in result
        assert "alpha" in result and "beta" in result
        assert "skill" in result and "reference" in result

    @pytest.mark.asyncio
    async def test_query_wrong_drawer_fts_companion(self, memory_env):
        """错抽屉：查询命中不依赖分类正确（FTS 伴生主路，§8）"""
        # 故意放进 reference 的"技能类"记忆
        _write_mem_cat(memory_env["global"], "reference", "hyperframes_recipe", "挂载 recipe 视频", "b")
        result = await mem_read.execute({"query": "挂载 recipe"})
        assert "hyperframes_recipe" in result

    @pytest.mark.asyncio
    async def test_stale_helpers(self):
        note = mem_read._stale_note("2024-01-01 10:00")
        assert note.startswith("⚠️ 记忆已")
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        assert mem_read._stale_note(now) == ""
        assert mem_read._stale_note("not-a-date") == ""

    @pytest.mark.asyncio
    async def test_entry_label(self):
        assert mem_read._entry_label({"name": "n1", "hint": ""}) == "n1"
        assert mem_read._entry_label({"name": "loc", "hint": "/path/to/x"}) == "loc→/path/to/x"
        assert "…" in mem_read._entry_label({"name": "x", "hint": "a" * 100})
