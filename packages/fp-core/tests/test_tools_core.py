"""测试核心工具 — bash / read_file / write_file / edit_file + ToolSpec 声明

覆盖重点：
- ToolSpec/ParamSpec 单一数据源：schema 生成、参数路由、必填校验
- read_file：默认/offset/limit/截断/哈希注册/文件不存在
- write_file：写入 + 哈希注册 + 旧条目清理
- edit_file：未注册/哈希不匹配/替换成功/找不到/多匹配/文件消失
- bash：正常/失败/大输出/超时/空命令
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fp_core.tools.core import (
    CORE_TOOLS,
    _compute_file_hash,
    _execute_bash,
    _execute_edit_file,
    _execute_read_file,
    _execute_write_file,
    _file_registry,
    execute_core_tool,
    get_core_definitions,
)


@pytest.fixture(autouse=True)
def clean_registry():
    """每个测试前清空文件注册表"""
    _file_registry.clear()
    yield
    _file_registry.clear()


# ═══════════════════════════════════════════════════════════
# 哈希
# ═══════════════════════════════════════════════════════════


class TestHash:
    def test_same_content_diff_path_diff_hash(self):
        assert _compute_file_hash("/a.txt", "x") != _compute_file_hash("/b.txt", "x")

    def test_same_path_diff_content_diff_hash(self):
        assert _compute_file_hash("/a.txt", "x") != _compute_file_hash("/a.txt", "y")

    def test_hash_is_6_chars(self):
        assert len(_compute_file_hash("/a", "b")) == 6


# ═══════════════════════════════════════════════════════════
# ToolSpec / ParamSpec
# ═══════════════════════════════════════════════════════════


class TestToolSpec:
    def test_to_openai_schema(self):
        spec = CORE_TOOLS[0]  # bash
        schema = spec.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "bash"
        assert "command" in schema["function"]["parameters"]["properties"]
        assert schema["function"]["parameters"]["required"] == ["command"]

    def test_run_missing_required(self):
        with pytest.raises(ValueError, match="缺少必填参数"):
            asyncio.run(CORE_TOOLS[0].run({}))

    @pytest.mark.asyncio
    async def test_run_routes_params(self):
        async def fake_handler(command):
            return f"cmd={command}"

        spec = CORE_TOOLS[0]
        spec.handler = fake_handler
        try:
            result = await spec.run({"command": "ls"})
            assert result == "cmd=ls"
        finally:
            spec.handler = _execute_bash


# ═══════════════════════════════════════════════════════════
# read_file
# ═══════════════════════════════════════════════════════════


class TestReadFile:
    @pytest.mark.asyncio
    async def test_read_basic(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("line1\nline2\nline3\n", encoding="utf-8")
        result = await _execute_read_file(str(p))
        assert "line1" in result
        assert "【文件哈希】" in result

    @pytest.mark.asyncio
    async def test_read_offset_limit(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("".join(f"line{i}\n" for i in range(10)), encoding="utf-8")
        result = await _execute_read_file(str(p), offset=2, limit=3)
        assert "line2" in result
        assert "line3" in result
        assert "line4" in result
        assert "line0" not in result
        # 截断提示剩余行
        assert "剩余 5 行" in result

    @pytest.mark.asyncio
    async def test_read_limit_capped_500(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("".join(f"l{i}\n" for i in range(600)), encoding="utf-8")
        result = await _execute_read_file(str(p), limit=9999)
        assert "剩余 100 行" in result  # 600 - 500

    @pytest.mark.asyncio
    async def test_read_registers_hash(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("hello", encoding="utf-8")
        result = await _execute_read_file(str(p))
        # 提取哈希并验证注册表
        h = result.strip().split("【文件哈希】")[-1].strip()
        assert _file_registry[h] == str(p)

    @pytest.mark.asyncio
    async def test_read_not_found(self):
        result = await _execute_read_file("/nonexistent/file.txt")
        assert "文件不存在" in result

    @pytest.mark.asyncio
    async def test_read_offset_beyond(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("line1\n", encoding="utf-8")
        result = await _execute_read_file(str(p), offset=100)
        assert "超出文件总行数" in result

    @pytest.mark.asyncio
    async def test_read_empty_path(self):
        with pytest.raises(ValueError, match="需要 file_path"):
            await _execute_read_file("")


# ═══════════════════════════════════════════════════════════
# write_file
# ═══════════════════════════════════════════════════════════


class TestWriteFile:
    @pytest.mark.asyncio
    async def test_write_creates_file_and_registers(self, tmp_path):
        p = tmp_path / "new" / "b.txt"
        result = await _execute_write_file(str(p), "内容")
        assert "文件已写入" in result
        assert os.path.exists(p)
        assert p.read_text(encoding="utf-8") == "内容"

        h = result.split("哈希: ")[-1].strip()
        assert _file_registry[h] == str(p)

    @pytest.mark.asyncio
    async def test_write_cleans_old_registry_entries(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("旧内容", encoding="utf-8")
        # 先注册旧内容哈希
        old_h = _compute_file_hash(str(p), "旧内容")
        _file_registry[old_h] = str(p)

        await _execute_write_file(str(p), "新内容")
        # 旧哈希被清理
        assert old_h not in _file_registry

    @pytest.mark.asyncio
    async def test_write_missing_params(self):
        with pytest.raises(ValueError, match="需要 file_path 和 content"):
            await _execute_write_file("", "x")


# ═══════════════════════════════════════════════════════════
# edit_file
# ═══════════════════════════════════════════════════════════


class TestEditFile:
    @pytest.mark.asyncio
    async def test_edit_unregistered_hash(self):
        result = await _execute_edit_file("abc123", "old", "new")
        assert "未在文件注册表中找到" in result

    @pytest.mark.asyncio
    async def test_edit_success(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("第一行\n目标文本\n第三行\n", encoding="utf-8")
        h = _compute_file_hash(str(p), p.read_text(encoding="utf-8"))
        _file_registry[h] = str(p)

        result = await _execute_edit_file(h, "目标文本", "替换后")
        assert "✅ 文件已修改" in result
        assert "替换后" in p.read_text(encoding="utf-8")
        # 新哈希已注册，旧哈希移除
        assert h not in _file_registry
        new_h = result.split("新哈希: ")[-1].strip()
        assert _file_registry[new_h] == str(p)

    @pytest.mark.asyncio
    async def test_edit_stale_hash_rejected(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("旧内容", encoding="utf-8")
        h = _compute_file_hash(str(p), "旧内容")
        _file_registry[h] = str(p)

        # 文件被外部修改
        p.write_text("被外部改了", encoding="utf-8")

        result = await _execute_edit_file(h, "旧内容", "新内容")
        assert "编辑被拒绝" in result
        assert "文件哈希不匹配" in result
        assert "被外部改了" in p.read_text(encoding="utf-8")  # 未改动

    @pytest.mark.asyncio
    async def test_edit_old_string_not_found(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("内容A", encoding="utf-8")
        h = _compute_file_hash(str(p), "内容A")
        _file_registry[h] = str(p)

        result = await _execute_edit_file(h, "不存在的文本", "x")
        assert "未找到要替换的文本" in result

    @pytest.mark.asyncio
    async def test_edit_multiple_matches_rejected(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("abc abc abc", encoding="utf-8")
        h = _compute_file_hash(str(p), "abc abc abc")
        _file_registry[h] = str(p)

        result = await _execute_edit_file(h, "abc", "X")
        assert "多个匹配项" in result

    @pytest.mark.asyncio
    async def test_edit_file_missing_removes_registry(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("x", encoding="utf-8")
        h = _compute_file_hash(str(p), "x")
        _file_registry[h] = str(p)
        os.remove(p)

        result = await _execute_edit_file(h, "x", "y")
        assert "文件不存在" in result
        assert h not in _file_registry

    @pytest.mark.asyncio
    async def test_edit_missing_params(self):
        with pytest.raises(ValueError, match="需要 file_hash"):
            await _execute_edit_file("", "old", "new")


# ═══════════════════════════════════════════════════════════
# bash
# ═══════════════════════════════════════════════════════════


class TestBash:
    @pytest.mark.asyncio
    async def test_empty_command_raises(self):
        with pytest.raises(ValueError, match="需要 command"):
            await _execute_bash("")

    @pytest.mark.asyncio
    async def test_success_small_output(self):
        result = await _execute_bash("echo hello")
        assert "hello" in result
        assert "✅" not in result  # 小输出不包装

    @pytest.mark.asyncio
    async def test_failure_output(self):
        result = await _execute_bash("exit 3")
        assert "❌ 命令执行失败" in result
        assert "exit=3" in result

    @pytest.mark.asyncio
    async def test_large_output_saved_to_file(self):
        result = await _execute_bash("seq 1 5000")
        assert "输出较长" in result
        assert "已保存至" in result
        assert "头部 200 字符" in result
        assert "尾部 200 字符" in result

    @pytest.mark.asyncio
    async def test_stderr_appended(self):
        result = await _execute_bash("echo out; echo err >&2")
        assert "[stderr]" in result

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self):
        """超时 → killpg 击杀进程组 + 返回超时错误"""
        with (
            patch("fp_core.tools.core.asyncio.wait_for", new_callable=AsyncMock, side_effect=asyncio.TimeoutError),
            patch("fp_core.tools.core._kill_process_group") as mock_kill,
            patch("fp_core.tools.core.asyncio.create_subprocess_shell") as mock_create,
        ):
            mock_proc = MagicMock()
            mock_proc.wait = AsyncMock(return_value=None)
            mock_create.return_value = mock_proc

            result = await _execute_bash("sleep 999")

        assert "命令执行超时" in result
        mock_kill.assert_awaited_once_with(mock_proc)

    @pytest.mark.asyncio
    async def test_exception_returns_error(self):
        with patch("fp_core.tools.core.asyncio.create_subprocess_shell", side_effect=OSError("boom")):
            result = await _execute_bash("anything")
        assert "错误：" in result


# ═══════════════════════════════════════════════════════════
# 对外接口
# ═══════════════════════════════════════════════════════════


class TestPublicAPI:
    def test_get_core_definitions(self):
        defs = get_core_definitions()
        names = {d["function"]["name"] for d in defs}
        assert names == {"bash", "read_file", "write_file", "edit_file"}

    @pytest.mark.asyncio
    async def test_execute_core_tool_unknown(self):
        with pytest.raises(ValueError, match="未知核心工具"):
            await execute_core_tool("nonexistent", {})

    @pytest.mark.asyncio
    async def test_execute_core_tool_bash(self):
        result = await execute_core_tool("bash", {"command": "echo hi"})
        assert "hi" in result
