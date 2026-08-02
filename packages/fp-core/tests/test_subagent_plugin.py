"""测试 subagent 插件 — 子 agent 派遣

覆盖重点：
- 参数校验：task/cwd 必填、cwd 存在性、timeout 边界
- 递归守卫（FP_IS_SUBAGENT=1）
- 成功/无输出/超时/启动失败 四条路径
- max_length 截断、output_format=json 转换
- store_result 保存记忆
- 辅助函数 _derive_summary_from_file / _finalize_subagent_session
"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import fp_core.tools.extensions.subagent_plugin as subagent
from fp_core.core.session import _generate_sid


class _FakeProc:
    """模拟 asyncio subprocess"""

    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.terminate = MagicMock()
        self.kill = MagicMock()
        self.wait = AsyncMock()
        self.communicate = AsyncMock(return_value=(self._stdout, self._stderr))


# ═══════════════════════════════════════════════════════════
# 参数校验
# ═══════════════════════════════════════════════════════════


class TestParamValidation:
    @pytest.mark.asyncio
    async def test_task_empty(self):
        result = json.loads(await subagent.execute({"task": "", "cwd": "/tmp"}))
        assert result["status"] == "error"
        assert "task 参数不能为空" in result["result"]

    @pytest.mark.asyncio
    async def test_cwd_empty(self):
        result = json.loads(await subagent.execute({"task": "x", "cwd": ""}))
        assert result["status"] == "error"
        assert "cwd 参数不能为空" in result["result"]

    @pytest.mark.asyncio
    async def test_cwd_not_exist(self):
        result = json.loads(await subagent.execute({"task": "x", "cwd": "/nonexistent/dir"}))
        assert result["status"] == "error"
        assert "cwd 目录不存在" in result["result"]

    @pytest.mark.asyncio
    async def test_timeout_bounds(self, tmp_path):
        """timeout 下限 10 秒、上限 900 秒"""
        captured = {}

        async def fake_create_subprocess_exec(*args, **kwargs):
            captured["timeout_arg"] = None
            return _FakeProc(stdout=b"done")

        with (
            patch.object(asyncio, "create_subprocess_exec", fake_create_subprocess_exec),
            patch("fp_core.core.session._generate_sid", return_value=_generate_sid()),
            patch("fp_core.core.session.get_current_session_id", return_value=""),
            patch("fp_core.tools.extensions.subagent_plugin._finalize_subagent_session"),
        ):
            await subagent.execute({"task": "t", "cwd": str(tmp_path), "timeout": 1})
            await subagent.execute({"task": "t", "cwd": str(tmp_path), "timeout": 5000})

        # 无法直接断言 wait_for 的参数；至少不抛错即可
        # 通过 patch wait_for 精确验证：
        pass

    @pytest.mark.asyncio
    async def test_timeout_clamped_via_wait_for(self, tmp_path):
        """验证传给 wait_for 的 timeout 被钳制在 [10,900]"""
        with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
            mock_wait.return_value = (b"ok", b"")
            with (
                patch.object(asyncio, "create_subprocess_exec", return_value=_FakeProc(stdout=b"ok")),
                patch("fp_core.core.session._generate_sid", return_value="s_test"),
                patch("fp_core.core.session.get_current_session_id", return_value=""),
                patch("fp_core.tools.extensions.subagent_plugin._finalize_subagent_session"),
            ):
                await subagent.execute({"task": "t", "cwd": str(tmp_path), "timeout": 1})
                first_timeout = mock_wait.await_args.kwargs.get("timeout")
                await subagent.execute({"task": "t", "cwd": str(tmp_path), "timeout": 5000})
                second_timeout = mock_wait.await_args.kwargs.get("timeout")

        assert first_timeout == 10
        assert second_timeout == 900

    @pytest.mark.asyncio
    async def test_recursion_guard(self, tmp_path):
        """FP_IS_SUBAGENT=1 时拒绝递归"""
        with patch.dict(os.environ, {"FP_IS_SUBAGENT": "1"}):
            result = json.loads(await subagent.execute({"task": "t", "cwd": str(tmp_path)}))
        assert result["status"] == "error"
        assert "递归调用被拒绝" in result["result"]


# ═══════════════════════════════════════════════════════════
# 执行路径
# ═══════════════════════════════════════════════════════════


class TestExecutePaths:
    @pytest.mark.asyncio
    async def test_success_returns_output(self, tmp_path):
        """成功时返回子进程 stdout 纯文本"""
        proc = _FakeProc(stdout="子 agent 的回复\n".encode())
        with (
            patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("fp_core.core.session._generate_sid", return_value="s_sub"),
            patch("fp_core.core.session.get_current_session_id", return_value="s_parent"),
            patch("fp_core.tools.extensions.subagent_plugin._finalize_subagent_session") as mock_finalize,
        ):
            result = await subagent.execute({"task": "帮我分析", "cwd": str(tmp_path), "context": "背景"})

        assert result == "子 agent 的回复"
        # 收尾被调用
        mock_finalize.assert_called_once()
        sid_arg = mock_finalize.call_args.args[0]
        assert sid_arg == "s_sub"

    @pytest.mark.asyncio
    async def test_success_sets_subagent_env(self, tmp_path):
        """子进程环境带防递归和静默标志"""
        captured = {}

        async def fake_create(*args, **kwargs):
            captured["env"] = kwargs.get("env")
            captured["cwd"] = kwargs.get("cwd")
            return _FakeProc(stdout=b"ok")

        with (
            patch.object(asyncio, "create_subprocess_exec", fake_create),
            patch("fp_core.core.session._generate_sid", return_value="s_sub"),
            patch("fp_core.core.session.get_current_session_id", return_value="s_parent"),
            patch("fp_core.tools.extensions.subagent_plugin._finalize_subagent_session"),
        ):
            await subagent.execute({"task": "t", "cwd": str(tmp_path)})

        assert captured["env"]["FP_IS_SUBAGENT"] == "1"
        assert captured["env"]["FP_SUBAGENT_SID"] == "s_sub"
        assert captured["env"]["FP_SUBAGENT_PARENT_SID"] == "s_parent"
        assert captured["cwd"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_no_output_warning(self, tmp_path):
        proc = _FakeProc(stdout=b"")
        with (
            patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("fp_core.core.session._generate_sid", return_value="s_sub"),
            patch("fp_core.core.session.get_current_session_id", return_value=""),
            patch("fp_core.tools.extensions.subagent_plugin._finalize_subagent_session"),
        ):
            result = json.loads(await subagent.execute({"task": "t", "cwd": str(tmp_path)}))

        assert result["status"] == "warning"
        assert "无输出" in result["result"]

    @pytest.mark.asyncio
    async def test_timeout_terminates_proc(self, tmp_path):
        """超时 → 优雅终止（terminate → 3s 后 kill）"""
        proc = _FakeProc(stdout=b"")
        proc.wait = AsyncMock(return_value=None)

        with (
            patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("asyncio.wait_for", new_callable=AsyncMock, side_effect=asyncio.TimeoutError),
            patch("fp_core.core.session._generate_sid", return_value="s_sub"),
            patch("fp_core.core.session.get_current_session_id", return_value=""),
            patch("fp_core.tools.extensions.subagent_plugin._finalize_subagent_session") as mock_finalize,
        ):
            result = json.loads(await subagent.execute({"task": "慢任务", "cwd": str(tmp_path), "timeout": 10}))

        proc.terminate.assert_called_once()
        assert result["status"] == "error"
        assert "超时" in result["result"]
        mock_finalize.assert_called_once()

    @pytest.mark.asyncio
    async def test_timeout_kill_after_grace(self, tmp_path):
        """3 秒宽限后仍不退 → SIGKILL"""
        proc = _FakeProc(stdout=b"")
        proc.wait = AsyncMock(side_effect=[asyncio.TimeoutError, asyncio.TimeoutError])

        with (
            patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("asyncio.wait_for", new_callable=AsyncMock, side_effect=asyncio.TimeoutError),
            patch("fp_core.core.session._generate_sid", return_value="s_sub"),
            patch("fp_core.core.session.get_current_session_id", return_value=""),
            patch("fp_core.tools.extensions.subagent_plugin._finalize_subagent_session"),
        ):
            json.loads(await subagent.execute({"task": "t", "cwd": str(tmp_path)}))

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_spawn_failure(self, tmp_path):
        """启动失败 → error 返回"""
        with (
            patch.object(asyncio, "create_subprocess_exec", AsyncMock(side_effect=OSError("no exec"))),
            patch("fp_core.core.session._generate_sid", return_value="s_sub"),
            patch("fp_core.core.session.get_current_session_id", return_value=""),
            patch("fp_core.tools.extensions.subagent_plugin._finalize_subagent_session"),
        ):
            result = json.loads(await subagent.execute({"task": "t", "cwd": str(tmp_path)}))

        assert result["status"] == "error"
        assert "启动失败" in result["result"]

    @pytest.mark.asyncio
    async def test_max_length_truncation(self, tmp_path):
        proc = _FakeProc(stdout=b"x" * 100)
        with (
            patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("fp_core.core.session._generate_sid", return_value="s_sub"),
            patch("fp_core.core.session.get_current_session_id", return_value=""),
            patch("fp_core.tools.extensions.subagent_plugin._finalize_subagent_session"),
        ):
            result = await subagent.execute({"task": "t", "cwd": str(tmp_path), "constraints": {"max_length": 10}})

        assert "已截断" in result
        assert len(result) < 100

    @pytest.mark.asyncio
    async def test_output_format_json_conversion(self, tmp_path):
        """output_format=json 且输出非 JSON → 包装为 JSON"""
        proc = _FakeProc(stdout=b"plain text")
        with (
            patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("fp_core.core.session._generate_sid", return_value="s_sub"),
            patch("fp_core.core.session.get_current_session_id", return_value=""),
            patch("fp_core.tools.extensions.subagent_plugin._finalize_subagent_session"),
        ):
            result = await subagent.execute({
                "task": "t",
                "cwd": str(tmp_path),
                "constraints": {"output_format": "json"},
            })

        parsed = json.loads(result)
        assert parsed["reply"] == "plain text"

    @pytest.mark.asyncio
    async def test_output_format_json_keeps_valid_json(self, tmp_path):
        proc = _FakeProc(stdout=b'{"valid": true}')
        with (
            patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("fp_core.core.session._generate_sid", return_value="s_sub"),
            patch("fp_core.core.session.get_current_session_id", return_value=""),
            patch("fp_core.tools.extensions.subagent_plugin._finalize_subagent_session"),
        ):
            result = await subagent.execute({
                "task": "t",
                "cwd": str(tmp_path),
                "constraints": {"output_format": "json"},
            })

        assert json.loads(result) == {"valid": True}

    @pytest.mark.asyncio
    async def test_store_result_saves_memory(self, tmp_path):
        """store_result 时调用 memory_save"""
        proc = _FakeProc(stdout="结果内容".encode())
        with (
            patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("fp_core.core.session._generate_sid", return_value="s_sub"),
            patch("fp_core.core.session.get_current_session_id", return_value=""),
            patch("fp_core.tools.extensions.subagent_plugin._finalize_subagent_session"),
            patch("fp_core.tools.extensions.memory_save_plugin.execute", new_callable=AsyncMock) as mock_save,
        ):
            result = await subagent.execute({"task": "t", "cwd": str(tmp_path), "store_result": "draft_x"})

        assert "结果内容" in result
        mock_save.assert_awaited_once()
        args = mock_save.await_args.args[0]
        assert args["name"] == "draft_x"
        assert args["content"] == "结果内容"


# ═══════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════


class TestHelpers:
    def test_derive_summary_from_file(self, tmp_path, monkeypatch):
        """从会话文件提取最后一条 user 消息"""
        sid = _generate_sid()
        path = os.path.join(str(tmp_path), f"{sid}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"__meta__": true}\n')
            f.write('{"role": "user", "content": "第一条"}\n')
            f.write('{"role": "assistant", "content": "回复"}\n')
            f.write('{"role": "user", "content": "最后一条 user 消息"}\n')

        with patch("fp_core.core.session.SESSIONS_DIR", str(tmp_path)):
            summary = subagent._derive_summary_from_file(sid)

        assert summary == "最后一条 user 消息"

    def test_derive_summary_from_file_missing(self, tmp_path):
        assert subagent._derive_summary_from_file("s_nonexistent") == ""

    def test_derive_summary_truncates_long(self, tmp_path):
        sid = _generate_sid()
        path = os.path.join(str(tmp_path), f"{sid}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"__meta__": true}\n')
            f.write('{"role": "user", "content": "' + "长" * 100 + '"}\n')
        with patch("fp_core.core.session.SESSIONS_DIR", str(tmp_path)):
            summary = subagent._derive_summary_from_file(sid)
        assert len(summary) <= 50

    def test_finalize_subagent_session(self, tmp_path):
        """收尾补写 source/parent_sid/summary"""
        sid = _generate_sid()
        path = os.path.join(str(tmp_path), f"{sid}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"__meta__": true}\n')
            f.write('{"role": "user", "content": "任务消息"}\n')

        with (
            patch("fp_core.core.session.SESSIONS_DIR", str(tmp_path)),
            patch("fp_core.core.session.update_session_meta") as mock_update,
        ):
            subagent._finalize_subagent_session(sid, "s_parent")

        mock_update.assert_called_once()
        args, kwargs = mock_update.call_args
        assert args[0] == sid
        assert kwargs["source"] == "subagent"
        assert kwargs["parent_sid"] == "s_parent"
        assert kwargs["summary"].startswith("[subagent] 任务消息")

    def test_finalize_uses_fallback_when_no_user_msg(self, tmp_path):
        """无 user 消息时用 fallback_summary"""
        sid = _generate_sid()
        path = os.path.join(str(tmp_path), f"{sid}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"__meta__": true}\n')

        with (
            patch("fp_core.core.session.SESSIONS_DIR", str(tmp_path)),
            patch("fp_core.core.session.update_session_meta") as mock_update,
        ):
            subagent._finalize_subagent_session(sid, "", fallback_summary="兜底任务描述")

        _, kwargs = mock_update.call_args
        assert kwargs["summary"].startswith("[subagent] 兜底任务描述")
