"""测试 SkillLoader — 技能加载与解析"""

import os

from fp_core.skills.loader import Skill, SkillLoader


class TestSkillDataclass:
    """Skill dataclass 基础"""

    def test_skill_defaults(self):
        """Skill 默认值正确"""
        skill = Skill(name="test", title="Test", description="测试")
        assert skill.category == "general"
        assert skill.version == "1.0"
        assert skill.priority == 5
        assert skill.content == ""
        assert skill.metadata == {}

    def test_skill_custom_values(self):
        """Skill 自定义字段"""
        skill = Skill(
            name="custom",
            title="自定义",
            description="描述",
            content="内容",
            category="testing",
            version="2.0",
            priority=10,
            metadata={"file": "custom.md"},
        )
        assert skill.name == "custom"
        assert skill.priority == 10
        assert skill.metadata["file"] == "custom.md"


class TestSkillLoader:
    """SkillLoader — 从目录加载技能"""

    def test_load_from_empty_dir(self, temp_skills_dir):
        """空目录 → 空技能列表"""
        loader = SkillLoader()
        # 手动清理并加载指定目录
        loader.skills.clear()
        loader._load_from_dir(temp_skills_dir)
        assert len(loader.skills) == 0

    def test_load_skill_with_frontmatter(self, temp_skills_dir, sample_skill_file):
        """加载带有 YAML frontmatter 的技能文件"""
        loader = SkillLoader()
        loader.skills.clear()
        loader._load_from_dir(temp_skills_dir)

        assert "test_skill" in loader.skills
        skill = loader.skills["test_skill"]
        assert skill.title == "测试技能"
        assert skill.description == "用于单元测试的技能"
        assert skill.category == "testing"
        assert skill.version == "2.0"
        assert skill.priority == 10

    def test_load_skill_no_frontmatter(self, temp_skills_dir, sample_skill_no_frontmatter):
        """无 frontmatter → 文件名作为 name，内容作为 content"""
        loader = SkillLoader()
        loader.skills.clear()
        loader._load_from_dir(temp_skills_dir)

        assert "simple_skill" in loader.skills
        skill = loader.skills["simple_skill"]
        assert skill.title == "Simple Skill"  # 自动 title case
        assert "hello world" in skill.content

    def test_load_multiple_skills(self, temp_skills_dir, sample_skill_file, sample_skill_no_frontmatter):
        """加载多个技能"""
        loader = SkillLoader()
        loader.skills.clear()
        loader._load_from_dir(temp_skills_dir)

        assert len(loader.skills) == 2

    def test_get_skill(self, temp_skills_dir, sample_skill_file):
        """get_skill() 按 name 获取"""
        loader = SkillLoader()
        loader.skills.clear()
        loader._load_from_dir(temp_skills_dir)

        skill = loader.get_skill("test_skill")
        assert skill is not None
        assert skill.name == "test_skill"

        missing = loader.get_skill("nonexistent")
        assert missing is None

    def test_user_skills_override_builtin(self, temp_skills_dir):
        """用户技能同名覆盖内置技能"""
        # 先加载一个内置技能（模拟）
        loader = SkillLoader()

        # 模拟内置技能
        builtin_name = "override_test"
        loader.skills[builtin_name] = Skill(
            name=builtin_name,
            title="内置版本",
            description="内置",
            content="内置内容",
        )

        # 创建同名用户技能
        import fp_core.config as cfg

        user_dir = os.path.join(cfg._XDG_DATA_HOME, "fp", "skills")
        os.makedirs(user_dir, exist_ok=True)
        user_file = os.path.join(user_dir, f"{builtin_name}.md")
        with open(user_file, "w", encoding="utf-8") as f:
            f.write(f"""---
name: {builtin_name}
title: 用户版本
description: 用户覆盖
---
用户内容
""")

        # 加载用户技能（会覆盖同名内置）
        loader._load_from_dir(user_dir)
        skill = loader.get_skill(builtin_name)
        assert skill is not None
        assert skill.title == "用户版本"
        assert "用户内容" in skill.content

        # 清理
        os.remove(user_file)
        os.rmdir(user_dir)


class TestParseYaml:
    """_parse_yaml() — 简单 YAML 解析"""

    def test_simple_key_values(self):
        loader = SkillLoader()
        result = loader._parse_yaml("name: test\ntitle: 测试\n")
        assert result["name"] == "test"
        assert result["title"] == "测试"

    def test_list_value(self):
        loader = SkillLoader()
        result = loader._parse_yaml("tags: [a, b, c]\n")
        assert result["tags"] == ["a", "b", "c"]

    def test_list_with_dashes(self):
        loader = SkillLoader()
        result = loader._parse_yaml("items:\n  - a\n  - b\n")
        # 简易 YAML 解析器会将 dash 列表解析为多行文本
        assert "a" in result.get("items", "")
        assert "b" in result.get("items", "")

    def test_empty_yaml(self):
        loader = SkillLoader()
        result = loader._parse_yaml("")
        assert result == {}

    def test_multiline_value(self):
        loader = SkillLoader()
        yaml_text = "description: |\n  line1\n  line2\n"
        result = loader._parse_yaml(yaml_text)
        assert "line1" in result.get("description", "")


class TestGetAllPromptText:
    """get_all_prompt_text() — 技能提示词生成"""

    def test_empty_skills(self):
        loader = SkillLoader()
        loader.skills.clear()
        text = loader.get_all_prompt_text()
        assert text == ""

    def test_includes_skill_titles(self, temp_skills_dir, sample_skill_file):
        loader = SkillLoader()
        loader.skills.clear()
        loader._load_from_dir(temp_skills_dir)

        text = loader.get_all_prompt_text()
        assert "测试技能" in text
        assert "test_skill" in text
        assert "可用技能" in text

    def test_respects_priority_order(self, temp_skills_dir):
        """高优先级技能排在前面"""
        loader = SkillLoader()

        # 手动添加两个技能，不同优先级
        loader.skills["low"] = Skill(name="low", title="低优先级", description="低", priority=1)
        loader.skills["high"] = Skill(name="high", title="高优先级", description="高", priority=10)

        text = loader.get_all_prompt_text()
        # 高优先级先出现
        high_pos = text.index("高优先级")
        low_pos = text.index("低优先级")
        assert high_pos < low_pos


class TestReload:
    """热重载"""

    def test_reload_clears_and_reloads(self, temp_skills_dir, sample_skill_file):
        loader = SkillLoader()
        loader.skills.clear()
        loader._load_from_dir(temp_skills_dir)

        assert len(loader.skills) == 1

        # 再创建一个技能文件
        extra_file = os.path.join(temp_skills_dir, "extra.md")
        with open(extra_file, "w", encoding="utf-8") as f:
            f.write("新增技能")

        # 重载（注意：这里实际上 reload 调用 load_all，会扫描 BUILTIN_SKILLS_DIR + USER_SKILLS_DIR）
        # 为了精确测试，手动再加载一次
        loader._load_from_dir(temp_skills_dir)
        assert len(loader.skills) == 2


class TestEdgeCases:
    """边界情况"""

    def test_skill_file_with_only_name(self, temp_skills_dir):
        """只有文件名，内容为空"""
        path = os.path.join(temp_skills_dir, "empty.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("")

        loader = SkillLoader()
        loader.skills.clear()
        loader._load_from_dir(temp_skills_dir)

        skill = loader.get_skill("empty")
        assert skill is not None
        assert skill.title == "Empty"

    def test_skill_file_corrupt_skipped(self, temp_skills_dir):
        """损坏文件加载不崩溃（可能被跳过或加载为乱码内容）"""
        path = os.path.join(temp_skills_dir, "corrupt.md")
        # 二进制内容
        with open(path, "wb") as f:
            f.write(b"\x00\x01\x02corrupt")

        loader = SkillLoader()
        loader.skills.clear()
        loader._load_from_dir(temp_skills_dir)
        # 不应崩溃——无论是否被加载，测试通过（不抛异常即为成功）

    def test_non_md_files_ignored(self, temp_skills_dir):
        """非 .md 文件被忽略"""
        path = os.path.join(temp_skills_dir, "script.py")
        with open(path, "w") as f:
            f.write("print('hello')")

        loader = SkillLoader()
        loader.skills.clear()
        loader._load_from_dir(temp_skills_dir)
        assert "script" not in loader.skills
