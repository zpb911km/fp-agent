"""task 命令(/task)测试 — 面向用户的控制台

覆盖: 看(list/show/view, 含前端分支) / 开(new) / 清(clear) /
人的特权(approve/reject/answer/pause·resume·abort) / 未知子命令;
以及插件 ON_INIT 注册 · on_unregister 清理的接线。
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fp_core.commands import get_command
from fp_core.core.lifecycle import LifecycleHook, LifecycleManager
from fp_core.plugins.task_system import TaskSystemPlugin
from fp_core.plugins.task_system import command as cmd
from fp_core.taskmap.models import MapStatus
from fp_core.taskmap.store import TaskMapStore


class _IO:
    def __init__(self, frontend: str):
        self.frontend = frontend


class _State:
    def __init__(self, frontend: str = "terminal"):
        self.io = _IO(frontend)


def _run(arg: str, frontend: str = "terminal") -> tuple[bool, str]:
    return asyncio.run(cmd.execute(_State(frontend), arg))


class TestTaskCommand(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.mkdtemp(prefix="taskcmd_")
        os.chdir(self._tmp)

    def tearDown(self):
        os.chdir(self._cwd)
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ── 看 ─────────────────────────────────────────

    def test_new_list_show(self):
        ok, out = _run("new 修扬声器")
        self.assertTrue(ok)
        self.assertIn("#1", out)
        _, out = _run("list")
        self.assertIn("修扬声器", out)
        _, out = _run("show 1")
        self.assertIn("n0", out)

    def test_view_branches_by_frontend(self):
        _run("new T")
        _, tree = _run("view 1", frontend="terminal")
        self.assertIn("#1", tree)
        self.assertNotIn("```mermaid", tree)
        _, mmd = _run("view 1", frontend="webui")
        self.assertIn("```mermaid", mmd)

    def test_view_explicit_flags_override_frontend(self):
        _run("new T")
        _, tree = _run("view 1 --tree", frontend="webui")
        self.assertNotIn("```mermaid", tree)
        _, mmd = _run("view 1 --mermaid", frontend="terminal")
        self.assertIn("```mermaid", mmd)

    def test_view_missing_id(self):
        _, out = _run("view 99")
        self.assertIn("未找到", out)

    def test_no_arg_lists(self):
        _, out = _run("")
        self.assertIn("暂无任务", out)

    # ── 人的特权 ────────────────────────────────────

    def test_answer_and_approve(self):
        _run("new T")
        st = TaskMapStore()
        m = st.get(1)
        m.questions.append({"q": "要拆机吗?", "resolved": False, "ts": 0})
        m.status = MapStatus.DELIVERED
        st.save_map(m)

        _, out = _run("answer 1 0 先别拆,先试软件")
        self.assertIn("已回应", out)
        q = TaskMapStore().get(1).questions[0]
        self.assertTrue(q["resolved"])
        self.assertEqual(q["answer"], "先别拆,先试软件")

        _, out = _run("approve 1")
        self.assertIn("已完成", out)
        self.assertEqual(TaskMapStore().get(1).status, MapStatus.COMPLETED)

    def test_answer_out_of_range(self):
        _run("new T")
        _, out = _run("answer 1 0 x")
        self.assertIn("越界", out)

    def test_reject_records_reason(self):
        _run("new T")
        st = TaskMapStore()
        m = st.get(1)
        m.status = MapStatus.DELIVERED
        st.save_map(m)
        _, out = _run("reject 1 没修好")
        self.assertIn("打回", out)
        m2 = TaskMapStore().get(1)
        self.assertEqual(m2.status, MapStatus.ACTIVE)
        self.assertTrue(any("没修好" in q.get("q", "") for q in m2.questions))

    def test_reject_guarded_when_not_delivered(self):
        _run("new T")
        _, out = _run("reject 1 x")
        self.assertIn("只有", out)

    def test_pause_resume_abort(self):
        _run("new T")
        _, out = _run("pause 1")
        self.assertIn("暂停", out)
        self.assertEqual(TaskMapStore().get(1).status, MapStatus.PAUSED)
        _, out = _run("resume 1")
        self.assertIn("恢复", out)
        self.assertEqual(TaskMapStore().get(1).status, MapStatus.ACTIVE)
        _, out = _run("abort 1")
        self.assertIn("作废", out)
        self.assertEqual(TaskMapStore().get(1).status, MapStatus.SUPERSEDED)

    def test_pause_guarded(self):
        _run("new T")
        _run("abort 1")
        _, out = _run("pause 1")
        self.assertIn("只有", out)

    def test_approve_aborted_refused(self):
        _run("new T")
        _run("abort 1")
        _, out = _run("approve 1")
        self.assertIn("作废", out)

    # ── 清 / 未知 ───────────────────────────────────

    def test_clear(self):
        _run("new A")
        _run("abort 1")
        _, out = _run("clear")
        self.assertIn("已清除", out)
        _, out = _run("list")
        self.assertIn("暂无任务", out)

    def test_unknown_subcommand_shows_help(self):
        _, out = _run("bogus")
        self.assertIn("未知子命令", out)
        self.assertIn("/task", out)


class TestPluginWiring(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.mkdtemp(prefix="taskwire_")
        os.chdir(self._tmp)

    def tearDown(self):
        os.chdir(self._cwd)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_on_init_registers_and_unregister_cleans(self):
        lc = LifecycleManager()
        p = TaskSystemPlugin()
        p.on_register(lc)
        asyncio.run(lc.emit(LifecycleHook.ON_INIT, tool_registry=None))
        self.assertIsNotNone(get_command("task"))
        p.on_unregister()
        self.assertIsNone(get_command("task"))

    def test_command_module_contract(self):
        self.assertEqual(cmd.name, "task")
        self.assertTrue(callable(cmd.execute))


if __name__ == "__main__":
    unittest.main()
