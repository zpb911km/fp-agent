"""
ToolExecutor — 工具执行层

职责：
- 持有一个 ToolRegistry 实例
- 提供 get_definitions() 返回 OpenAI schema
- 提供 execute(tool_call) 执行工具调用
- 不处理 IO、不显示工具调用信息
- 只做"工具调用→结果"的纯执行
"""

import json
from typing import Any


class ToolExecutor:
    """工具执行器"""

    def __init__(self, registry: Any | None = None):
        """
        Args:
            registry: ToolRegistry 实例。None 时创建独立实例（不再共享全局单例）
        """
        if registry is not None:
            self._registry = registry
        else:
            from fp_core.tools import create_registry

            self._registry = create_registry()

    def get_definitions(self) -> list[dict]:
        """获取所有工具的 OpenAI function calling schema"""
        return self._registry.get_all_definitions()

    async def execute(self, tool_call: dict) -> str:
        """
        执行工具调用。

        异常策略：不吞异常，所有工具执行异常向上传播。
        由 Agent 层的 _execute_one_tool 捕获并触发 ON_TOOL_ERROR 生命周期。

        Args:
            tool_call: tool_call dict，格式:
                {"id": "...", "type": "function",
                 "function": {"name": "...", "arguments": "..."}}

        Returns:
            执行结果的字符串表示

        Raises:
            json.JSONDecodeError: 工具参数 JSON 解析失败
            TypeError: 工具参数类型错误
            Exception: 工具执行时的其他异常
        """
        name = tool_call["function"]["name"]
        args = json.loads(tool_call["function"]["arguments"])
        result = await self._registry.execute(name, args)
        return str(result) if result is not None else "执行成功（无返回）"
