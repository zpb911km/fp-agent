"""测试 subagent **资产**（三来源用户资产，不再是 core 内置工具）。

为什么被测代码不在本仓库、测试却住在这里
========================================
subagent 已从 ``fp_core/tools/extensions/`` 搬到三来源资产目录（默认 public）。
它不再属于本仓库，所以本文件**不能** ``import fp_core.tools.extensions.subagent_plugin``
——那等于把「核心测试依赖某个可选资产」重新写死回来（正是这次搬迁要拆的耦合）。

改为按三来源路径**动态加载**磁盘上那份资产；四个来源目录都没有时整个文件
skip。本机（装了资产）覆盖真实实现，CI/他人机器（没装）不会假失败——
被测代码缺席时跳过是诚实的：仓库本来就不负责分发它。

覆盖语义（与搬迁前一致，另加搬迁引入的新契约）：
- 参数校验：task/cwd 必填、cwd 存在性、timeout 边界（下限 10s / 缺省无限）
- 递归守卫：``fp_core.spawn.MAX_DEPTH``（不再是 FP_IS_SUBAGENT 布尔）
- **入口声明**：前端未声明 ``FP_SPAWN_ENTRY`` 时给可读拒绝，而不是 traceback
- **spawn 契约**：子进程环境由 ``fp_core.spawn.child_env`` 构造（深度/sid/来源/静默）
- 成功/无输出/超时/启动失败 四条路径
- max_length 截断、output_format=json 转换、store_result 保存记忆
- 辅助函数 ``_derive_summary_from_file`` / ``_finalize_session``
"""

import asyncio
import importlib.util
import json
import os
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fp_core.config import EXT_LAYOUTS, EXT_SOURCES, user_dirs


def _asset_roots() -> list[str]:
    """三来源目录；外加「本机真实用户目录」下的同三来源（若根 conftest 给了）。

    为什么需要后半截：根 ``conftest.py`` 会把用户目录钉到本次 run 的临时目录
    （见 ``docs/dev/测试.md`` § 隔离用户目录），于是 ``user_dirs()`` 只剩空目录。
    但本文件的语义恰恰是「**本机装了**资产就测真实实现，没装就跳过」——隔离不该
    把它变成恒跳过（一次全量跑实测：23 个用例从 passed 静默变 skipped）。
    根 conftest 钉住前会把真实数据根导出到 ``FP_TEST_REAL_DATA_DIR``，这里补进来。
    """
    roots = list(user_dirs("tools"))
    real = os.environ.get("FP_TEST_REAL_DATA_DIR")
    if real:
        roots += [os.path.join(real, s, *EXT_LAYOUTS["tools"]) for s in EXT_SOURCES]
    return roots


def _load_asset():
    """按三来源优先级（private → public → fetched）加载磁盘上的 subagent 资产。"""
    for d in reversed(_asset_roots()):
        path = os.path.join(d, "subagent_plugin.py")
        if not os.path.isfile(path):
            continue
        spec = importlib.util.spec_from_file_location("fp_subagent_asset", path)
        if spec is None or spec.loader is None:  # pragma: no cover - 环境异常
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules["fp_subagent_asset"] = module
        spec.loader.exec_module(module)
        return module
    return None


subagent = _load_asset()

pytestmark = pytest.mark.skipif(
    subagent is None,
    reason="subagent 资产未安装（fetched/public/private 均无 tools/extensions/subagent_plugin.py）",
)


@pytest.fixture(autouse=True)
def _spawn_contract(monkeypatch):
    """隔离 spawn 契约：默认「顶层实例 + 前端已声明入口」。

    递归守卫读的是 ``FP_SPAWN_DEPTH``（数值），入口读的是 ``FP_SPAWN_ENTRY``。
    外部若真在派遣链里跑测试（深度 >= 1），不清掉会整文件假失败。
    """
    monkeypatch.delenv("FP_SPAWN_DEPTH", raising=False)
    monkeypatch.setenv("FP_SPAWN_ENTRY", json.dumps([sys.executable, "-m", "fp_cli.main", "-m"]))


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


async def _timeout_wait_for(aw: Any, *args: Any, **kwargs: Any) -> Any:
    """模拟 wait_for 超时：先关闭传入协程（否则 AsyncMock 协程泄漏
    "never awaited" 警告），再抛 TimeoutError 走生产码的超时分支。"""
    if asyncio.iscoroutine(aw):
        aw.close()
    raise TimeoutError


def _patched_run(tmp_path, proc, *, sid="s_sub", parent="", **extra):
    """把一次 execute 需要的打桩收进一处（entry 之外的 patch 都在这里）。"""
    return (
        patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
        patch("fp_core.core.session._generate_sid", return_value=sid),
        patch("fp_core.core.session.get_current_session_id", return_value=parent),
        patch.object(subagent, "_finalize_session"),
        *extra,
    )


# ═══════════════════════════════════════════════════════════
# 参数校验
# ═══════════════════════════════════════════════════════════


class TestParamValidation:
    @pytest.mark.asyncio
    async def test_task_empty(self):
        result = json.loads(await subagent.execute({"task": "", "cwd": "/tmp"}))
        assert result["status"] == "error"
        assert "task 不能为空" in result["result"]

    @pytest.mark.asyncio
    async def test_cwd_empty(self):
        result = json.loads(await subagent.execute({"task": "x", "cwd": ""}))
        assert result["status"] == "error"
        assert "cwd 不能为空" in result["result"]

    @pytest.mark.asyncio
    async def test_cwd_not_exist(self):
        result = json.loads(await subagent.execute({"task": "x", "cwd": "/nonexistent/dir"}))
        assert result["status"] == "error"
        assert "cwd 目录不存在" in result["result"]

    @pytest.mark.asyncio
    async def test_timeout_clamped_via_wait_for(self, tmp_path):
        """验证传给 wait_for 的 timeout：下限 10 秒、无上限、缺省 None（无限）"""
        timeouts: list[float | None] = []

        # 用真 async 函数替代 AsyncMock：mock 不会 await 传入的
        # proc.communicate() 协程，会泄漏 "never awaited" 警告
        async def fake_wait_for(aw: Any, *, timeout: float | None = None, **_kw: Any) -> Any:
            timeouts.append(timeout)
            return await aw

        with (
            patch("asyncio.wait_for", fake_wait_for),
            patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=_FakeProc(stdout=b"ok"))),
            patch("fp_core.core.session._generate_sid", return_value="s_test"),
            patch("fp_core.core.session.get_current_session_id", return_value=""),
            patch.object(subagent, "_finalize_session"),
        ):
            await subagent.execute({"task": "t", "cwd": str(tmp_path), "timeout": 1})
            await subagent.execute({"task": "t", "cwd": str(tmp_path), "timeout": 5000})
            await subagent.execute({"task": "t", "cwd": str(tmp_path)})

        assert timeouts == [10, 5000, None]


# ═══════════════════════════════════════════════════════════
# spawn 契约（搬迁引入的两条新行为）
# ═══════════════════════════════════════════════════════════


class TestSpawnContract:
    @pytest.mark.asyncio
    async def test_recursion_guard_uses_depth(self, tmp_path, monkeypatch):
        """深度已达上限 → 拒绝；判据是 FP_SPAWN_DEPTH，不是身份布尔。"""
        monkeypatch.setenv("FP_SPAWN_DEPTH", "1")
        result = json.loads(await subagent.execute({"task": "t", "cwd": str(tmp_path)}))
        assert result["status"] == "error"
        assert "递归调用被拒绝" in result["result"]
        # 错误信息要说清现场（深度/上限），否则排查只能靠猜
        assert "深度 1" in result["result"]

    @pytest.mark.asyncio
    async def test_missing_entry_is_readable_error(self, tmp_path, monkeypatch):
        """前端没声明拉起入口 → 可读拒绝（不是 traceback、不是空跑）。"""
        monkeypatch.delenv("FP_SPAWN_ENTRY", raising=False)
        result = json.loads(await subagent.execute({"task": "t", "cwd": str(tmp_path)}))
        assert result["status"] == "error"
        assert "FP_SPAWN_ENTRY" in result["result"]

    @pytest.mark.asyncio
    async def test_child_env_and_argv(self, tmp_path):
        """子进程环境由 fp_core.spawn.child_env 构造；argv = 前端声明入口 + 提示词。"""
        captured: dict[str, Any] = {}

        async def fake_create(*args, **kwargs):
            captured["argv"] = args
            captured["env"] = kwargs.get("env")
            captured["cwd"] = kwargs.get("cwd")
            return _FakeProc(stdout=b"ok")

        with (
            patch.object(asyncio, "create_subprocess_exec", fake_create),
            patch("fp_core.core.session._generate_sid", return_value="s_sub"),
            patch("fp_core.core.session.get_current_session_id", return_value="s_parent"),
            patch.object(subagent, "_finalize_session"),
        ):
            await subagent.execute({"task": "分析日志", "cwd": str(tmp_path), "context": "背景"})

        env = captured["env"]
        assert env["FP_SPAWN_DEPTH"] == "1"  # 顶层 0 → 子 1
        assert env["FP_SPAWN_SID"] == "s_sub"
        assert env["FP_SPAWN_PARENT_SID"] == "s_parent"
        assert env["FP_SPAWN_SOURCE"] == "subagent"
        assert env["FP_QUIET"] == "1"
        assert env["FP_SILENT"] == "1"  # 非 verbose → 静默
        assert captured["cwd"] == str(tmp_path)
        # argv：前端声明的入口前缀 + 一条提示词
        assert captured["argv"][-2:] == ("-m", "[背景信息]\n背景\n\n[任务]\n分析日志")

    @pytest.mark.asyncio
    async def test_verbose_keeps_silent_off(self, tmp_path):
        """constraints.verbose=True → 不设 FP_SILENT（子实例照常渲染过程）。"""
        captured: dict[str, Any] = {}

        async def fake_create(*args, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FakeProc(stdout=b"ok")

        with (
            patch.object(asyncio, "create_subprocess_exec", fake_create),
            patch("fp_core.core.session._generate_sid", return_value="s_sub"),
            patch("fp_core.core.session.get_current_session_id", return_value=""),
            patch.object(subagent, "_finalize_session"),
        ):
            await subagent.execute({"task": "t", "cwd": str(tmp_path), "constraints": {"verbose": True}})

        assert "FP_SILENT" not in captured["env"]


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
            patch.object(subagent, "_finalize_session") as mock_finalize,
        ):
            result = await subagent.execute({"task": "帮我分析", "cwd": str(tmp_path), "context": "背景"})

        assert result == "子 agent 的回复"
        # 收尾被调用，且父进程预分配的 sid 传对了
        mock_finalize.assert_called_once()
        assert mock_finalize.call_args.args[0] == "s_sub"

    @pytest.mark.asyncio
    async def test_no_output_warning(self, tmp_path):
        proc = _FakeProc(stdout=b"")
        with (
            patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("fp_core.core.session._generate_sid", return_value="s_sub"),
            patch("fp_core.core.session.get_current_session_id", return_value=""),
            patch.object(subagent, "_finalize_session"),
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
            patch("asyncio.wait_for", _timeout_wait_for),
            patch("fp_core.core.session._generate_sid", return_value="s_sub"),
            patch("fp_core.core.session.get_current_session_id", return_value=""),
            patch.object(subagent, "_finalize_session") as mock_finalize,
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
            patch("asyncio.wait_for", _timeout_wait_for),
            patch("fp_core.core.session._generate_sid", return_value="s_sub"),
            patch("fp_core.core.session.get_current_session_id", return_value=""),
            patch.object(subagent, "_finalize_session"),
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
            patch.object(subagent, "_finalize_session"),
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
            patch.object(subagent, "_finalize_session"),
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
            patch.object(subagent, "_finalize_session"),
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
            patch.object(subagent, "_finalize_session"),
        ):
            result = await subagent.execute({
                "task": "t",
                "cwd": str(tmp_path),
                "constraints": {"output_format": "json"},
            })

        assert json.loads(result) == {"valid": True}

    @pytest.mark.asyncio
    async def test_store_result_saves_memory(self, tmp_path):
        """store_result 时按名经 L1 工具注册表调用 memory_save（不 import 兄弟扩展）"""
        proc = _FakeProc(stdout="结果内容".encode())
        with (
            patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("fp_core.core.session._generate_sid", return_value="s_sub"),
            patch("fp_core.core.session.get_current_session_id", return_value=""),
            patch.object(subagent, "_finalize_session"),
            patch("fp_core.tools.execute_tool", new_callable=AsyncMock) as mock_exec,
        ):
            result = await subagent.execute({"task": "t", "cwd": str(tmp_path), "store_result": "draft_x"})

        assert "结果内容" in result
        mock_exec.assert_awaited_once()
        assert mock_exec.await_args is not None
        tool_name, params = mock_exec.await_args.args[0], mock_exec.await_args.args[1]
        assert tool_name == "memory_save"
        assert params["name"] == "draft_x"
        assert params["content"] == "结果内容"
        assert params["root"] == "~"
        assert params["category"] == "reference"


# ═══════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════


class TestHelpers:
    def test_derive_summary_from_file(self, tmp_path):
        """kw 为空时回退末条 user，并受 20 字上限约束（与核心同策略）"""
        path = os.path.join(str(tmp_path), "s_x.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"__meta__": true}\n')
            f.write('{"role": "user", "content": "第一条"}\n')
            f.write('{"role": "assistant", "content": "回复"}\n')
            f.write('{"role": "user", "content": "最后一条 user 消息"}\n')

        with patch("fp_core.core.session.SESSIONS_DIR", str(tmp_path)):
            summary = subagent._derive_summary_from_file("s_x")

        assert summary == "最后一条 user 消息"

    def test_derive_summary_from_file_missing(self):
        assert subagent._derive_summary_from_file("s_nonexistent_xyz") == ""

    def test_derive_summary_truncates_long(self, tmp_path):
        path = os.path.join(str(tmp_path), "s_y.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"__meta__": true}\n')
            f.write('{"role": "user", "content": "' + "长" * 100 + '"}\n')
        with patch("fp_core.core.session.SESSIONS_DIR", str(tmp_path)):
            summary = subagent._derive_summary_from_file("s_y")
        assert len(summary) <= 20

    def test_finalize_writes_meta(self, tmp_path):
        """收尾补写 source/parent_sid/summary（摘要带 [subagent] 标签）"""
        path = os.path.join(str(tmp_path), "s_z.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"__meta__": true}\n')
            f.write('{"role": "user", "content": "任务消息"}\n')

        with (
            patch("fp_core.core.session.SESSIONS_DIR", str(tmp_path)),
            patch("fp_core.core.session.update_session_meta") as mock_update,
        ):
            subagent._finalize_session("s_z", "s_parent")

        mock_update.assert_called_once()
        args, kwargs = mock_update.call_args
        assert args[0] == "s_z"
        assert kwargs["source"] == "subagent"
        assert kwargs["parent_sid"] == "s_parent"
        assert kwargs["summary"].startswith("[subagent] 任务消息")

    def test_finalize_uses_fallback_when_no_user_msg(self, tmp_path):
        """无 user 消息时用 fallback_summary"""
        path = os.path.join(str(tmp_path), "s_w.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"__meta__": true}\n')

        with (
            patch("fp_core.core.session.SESSIONS_DIR", str(tmp_path)),
            patch("fp_core.core.session.update_session_meta") as mock_update,
        ):
            subagent._finalize_session("s_w", "", fallback_summary="兜底任务描述")

        _, kwargs = mock_update.call_args
        assert kwargs["summary"].startswith("[subagent] 兜底任务描述")

    def test_finalize_skips_missing_session_file(self, tmp_path):
        """子实例从未落盘 → 不造 0 长度会话文件、不写 meta（列表噪声为零）"""
        with (
            patch("fp_core.core.session.SESSIONS_DIR", str(tmp_path)),
            patch("fp_core.core.session.update_session_meta") as mock_update,
        ):
            subagent._finalize_session("s_never_existed", "s_parent", fallback_summary="x")

        mock_update.assert_not_called()
        assert not os.path.exists(os.path.join(str(tmp_path), "s_never_existed.jsonl"))
