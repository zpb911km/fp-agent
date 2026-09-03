"""
Task System Plugin 集成测试

验证：
1. 插件通过 PluginRegistry 自动扫描加载
2. ON_INIT 中注册工具 + 注入 system prompt
4. 工具可被 ToolRegistry 正常调用
"""

import os
import sys
import tempfile
import unittest

# ── 确保 src 在 sys.path ──
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fp_core.core.lifecycle import LifecycleHook, LifecycleManager
from fp_core.plugins.base.plugin import PluginRegistry
from fp_core.plugins.task_system import TaskSystemPlugin
from fp_core.plugins.task_system.store import TaskStore
from fp_core.plugins.task_system.tools import handle_clear, handle_create, handle_list, handle_update
from fp_core.tools import ToolRegistry


class TestTaskSystemPlugin(unittest.IsolatedAsyncioTestCase):
    """TaskSystemPlugin 集成测试"""

    def setUp(self):
        # 使用临时目录隔离测试
        self._orig_cwd = os.getcwd()
        self._tmpdir = tempfile.mkdtemp(prefix="fp_test_task_")
        os.chdir(self._tmpdir)
        # 创建 .fp 目录
        os.makedirs(".fp")

    def tearDown(self):
        os.chdir(self._orig_cwd)
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_mock_registry(self):
        """创建一个可注入的 mock ToolRegistry（不自动扫描 plugins 目录）"""
        registry = ToolRegistry()
        # 清空已有插件（只保留 core 工具）
        registry._plugins.clear()
        return registry

    # ── 测试 1: 插件加载 ───────────────────────────

    def test_plugin_instantiation(self):
        """确保插件可以被实例化"""
        plugin = TaskSystemPlugin()
        self.assertEqual(plugin.name, "task_system")
        self.assertTrue(plugin.is_enabled)

    def test_plugin_scan_discovery(self):
        """确保插件可以被 PluginRegistry 自动扫描发现"""
        lifecycle = LifecycleManager()
        plugin_dir = os.path.join(os.path.dirname(__file__), "..", "src", "fp_core", "plugins")
        registry = PluginRegistry(lifecycle, plugin_dir=plugin_dir)
        plugins = registry.list_plugins()
        self.assertIn("task_system", plugins, f"task_system 插件应被扫描到，已注册: {plugins}")

    # ── 测试 2: ON_INIT 工具注册 ───────────────────

    async def test_on_init_registers_tools(self):
        """ON_INIT 钩子应通过 tool_registry 注册 4 个工具"""
        lifecycle = LifecycleManager()
        plugin = TaskSystemPlugin()
        plugin.on_register(lifecycle)

        # 创建 mock registry 并注入
        mock_registry = self._make_mock_registry()
        self.assertEqual(len(mock_registry.get_all_definitions()), 4)  # 只有 core 工具

        # 触发 ON_INIT
        ctx = await lifecycle.emit(LifecycleHook.ON_INIT, tool_registry=mock_registry)

        # 验证工具已注册
        defs = mock_registry.get_all_definitions()
        tool_names = [d["function"]["name"] for d in defs]
        self.assertIn("task_create", tool_names)
        self.assertIn("task_update", tool_names)
        self.assertIn("task_list", tool_names)
        self.assertIn("task_clear", tool_names)

        # 验证 system_prompt_append（list 收集语义：任一段含关键词即可）
        self.assertIn("system_prompt_append", ctx.data)
        append_parts = ctx.data["system_prompt_append"]
        self.assertIsInstance(append_parts, list)
        self.assertTrue(any("task_create" in part for part in append_parts))

    async def test_on_before_llm_call_adds_hint(self):
        lifecycle = LifecycleManager()
        plugin = TaskSystemPlugin()
        plugin.on_register(lifecycle)

        # 先创建一个进行中任务
        store = TaskStore()
        store.create("测试任务 1")
        t2 = store.create("测试任务 2")
        store.update(t2.id, "in_progress")

        # 触发 ON_BEFORE_LLM_CALL
        msgs = [{"role": "system", "content": "test prompt"}]
        ctx = await lifecycle.emit(LifecycleHook.ON_BEFORE_LLM_CALL, messages=msgs, tools=[])

        modified = ctx.data.get("modified_messages", [])
        last_msg = modified[-1]
        self.assertEqual(last_msg["role"], "system")
        # 应有 ▶#2（进行中）和 ⬜1（待办）
        self.assertIn("▶#2", last_msg["content"])
        self.assertIn("⬜1", last_msg["content"])

    async def test_on_before_llm_call_no_tasks(self):
        lifecycle = LifecycleManager()
        plugin = TaskSystemPlugin()
        plugin.on_register(lifecycle)

        # 先触发 ON_INIT（真实流程中 ON_INIT 总是在 ON_BEFORE_LLM_CALL 之前）
        await lifecycle.emit(LifecycleHook.ON_INIT, tool_registry=None)

        msgs = [{"role": "system", "content": "test prompt"}]
        ctx = await lifecycle.emit(LifecycleHook.ON_BEFORE_LLM_CALL, messages=msgs, tools=[])

        modified = ctx.data.get("modified_messages")
        # 无任务时不应设置 modified_messages
        self.assertIsNone(modified)

    async def test_on_before_llm_call_all_done(self):
        lifecycle = LifecycleManager()
        plugin = TaskSystemPlugin()
        plugin.on_register(lifecycle)

        # 先触发 ON_INIT（真实流程中 ON_INIT 总是在 ON_BEFORE_LLM_CALL 之前）
        await lifecycle.emit(LifecycleHook.ON_INIT, tool_registry=None)

        store = TaskStore()
        t = store.create("已完成任务")
        store.update(t.id, "completed")

        msgs = [{"role": "system", "content": "test prompt"}]
        ctx = await lifecycle.emit(LifecycleHook.ON_BEFORE_LLM_CALL, messages=msgs, tools=[])

        modified = ctx.data.get("modified_messages")
        self.assertIsNone(modified)

    # ── 测试 4: 工具功能 ───────────────────────────

    async def test_tool_create(self):
        """task_create 工具应创建任务并持久化"""
        result = await handle_create({"subject": "实现登录模块"})
        self.assertIn("✅ 已创建任务", result)
        self.assertIn("实现登录模块", result)

        store = TaskStore()
        tasks = store.list_all()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].subject, "实现登录模块")

    async def test_tool_update(self):
        """task_update 工具应更新任务状态"""
        store = TaskStore()
        t = store.create("测试任务")

        result = await handle_update({"task_id": t.id, "status": "in_progress"})
        self.assertIn("✅ 任务", result)
        self.assertIn("in_progress", result)

        tasks = store.list_all()
        self.assertEqual(tasks[0].status.value, "in_progress")

    async def test_tool_update_str_id(self):
        """回归：task_id 以字符串传入（如 "2"）也应能匹配到任务

        之前 store.update 用 int == str 严格比较，LLM 把整数 id
        序列化成字符串时会导致"未找到任务"误报。
        """
        store = TaskStore()
        t = store.create("测试任务")

        result = await handle_update({"task_id": str(t.id), "status": "in_progress"})
        self.assertIn("✅ 任务", result)
        self.assertIn("in_progress", result)

        tasks = store.list_all()
        self.assertEqual(tasks[0].status.value, "in_progress")

    async def test_tool_update_float_id(self):
        """回归：task_id 以 float 形式传入（如 2.0）也应能匹配到任务"""
        store = TaskStore()
        t = store.create("测试任务")

        result = await handle_update({"task_id": float(t.id), "status": "completed"})
        self.assertIn("✅ 任务", result)

        tasks = store.list_all()
        self.assertEqual(tasks[0].status.value, "completed")

    async def test_tool_list(self):
        """task_list 工具应列出所有任务"""
        store = TaskStore()
        store.create("任务 A")
        store.create("任务 B")

        result = await handle_list({})
        self.assertIn("📋 任务列表", result)
        self.assertIn("任务 A", result)
        self.assertIn("任务 B", result)

    async def test_tool_list_empty(self):
        """无任务时 task_list 应返回提示"""
        result = await handle_list({})
        self.assertEqual(result, "暂无任务")

    async def test_tool_clear(self):
        """task_clear 应清除已完成任务"""
        store = TaskStore()
        t1 = store.create("待办任务")
        t2 = store.create("已完成任务")
        store.update(t2.id, "completed")

        result = await handle_clear({})
        self.assertIn("已清除 1 个", result)

        tasks = store.list_all()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].id, t1.id)

    async def test_tool_clear_none(self):
        """无已完成任务时 task_clear 应提示"""
        store = TaskStore()
        store.create("待办任务")

        result = await handle_clear({})
        self.assertIn("没有已完成的任务", result)

    # ── 测试 5: 通过 ToolRegistry 调用 ──────────────

    async def test_tool_registry_integration(self):
        """通过 ToolRegistry.execute 调用 task 工具"""
        registry = self._make_mock_registry()

        # 手动注册
        from fp_core.plugins.task_system.tools import DEF_CREATE, DEF_LIST

        registry.register_tool("task_create", DEF_CREATE, handle_create)
        registry.register_tool("task_list", DEF_LIST, handle_list)

        # 调用 task_create
        result = await registry.execute("task_create", {"subject": "通过 registry 创建"})
        self.assertIn("✅ 已创建任务", result)

        # 调用 task_list
        result = await registry.execute("task_list", {})
        self.assertIn("通过 registry 创建", result)

    # ── 测试 6: system_prompt_append ────────────────

    async def test_system_prompt_append_content(self):
        """ON_INIT 应通过 context 返回 system_prompt_append（list 收集）"""
        lifecycle = LifecycleManager()
        plugin = TaskSystemPlugin()
        plugin.on_register(lifecycle)

        ctx = await lifecycle.emit(LifecycleHook.ON_INIT)
        append_parts = ctx.data.get("system_prompt_append", [])
        self.assertIsInstance(append_parts, list)
        joined = "\n".join(append_parts)
        self.assertIn("task_create", joined)
        self.assertIn("task_update", joined)
        self.assertIn("task_list", joined)
        self.assertIn("task_clear", joined)


if __name__ == "__main__":
    unittest.main()
