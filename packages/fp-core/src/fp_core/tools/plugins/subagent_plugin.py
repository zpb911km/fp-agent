"""
Subagent 插件 — 派遣子 agent 执行独立任务（异步版本）

主 agent 可以派遣子 agent 处理独立子任务，子 agent 拥有独立的会话上下文，
其全部思考过程和工具调用都不占用主上下文的 token 空间。
执行结束后返回结果摘要。

架构哲学：
  - 主 agent = 指挥官（分解问题、分配任务、合成结果）
  - 子 agent = 士兵（专注执行单一子任务）
  - 记忆（memory）是共享的白板，主/子 agent 均可读写

防递归：子 agent 不可再次创建子 agent（环境变量守卫 + 执行时拒绝）
"""

import asyncio
import json
import os
import sys
import time
from typing import Any

# ── 插件定义（OpenAI function calling schema） ──────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "subagent",
        "description": "派遣子 agent 执行独立任务，子 agent 有独立上下文，其工具调用和中间推理不占主上下文 token。"
        "适合多步分析、文件处理、代码调试等。超过 2 步工具调用或读大文件时使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "任务描述，需清晰完整自包含",
                },
                "cwd": {
                    "type": "string",
                    "description": "必填。工作目录，通过 bash(pwd) 获取",
                },
                "context": {
                    "type": "string",
                    "description": "可选。背景上下文：文件路径、代码片段、前置结论等。子 agent 无对话历史",
                },
                "store_result": {
                    "type": "string",
                    "description": "可选。结果自动存入记忆，之后 memory_read 读取。如 'draft_content'",
                },
                "timeout": {
                    "type": "integer",
                    "description": "可选。超时秒数，默认 300，范围 10~900",
                },
                "constraints": {
                    "type": "object",
                    "description": "可选。输出契约",
                    "properties": {
                        "verbose": {
                            "type": "boolean",
                            "description": "默认 false，只输出结论；true 输出完整推理链",
                        },
                        "output_format": {
                            "type": "string",
                            "enum": ["text", "json", "markdown"],
                            "description": "输出格式，默认 text",
                        },
                        "max_length": {
                            "type": "integer",
                            "description": "结果最大字符数，默认不限制",
                        },
                    },
                },
            },
            "required": ["task", "cwd"],
        },
    },
}


async def execute(params: dict[str, Any]) -> str:
    """
    执行子 agent 任务（异步）

    Args:
        params: 包含 task, cwd, context, store_result, timeout, constraints 的字典

    Returns:
        JSON 格式的执行结果
    """
    task = params.get("task", "")
    cwd = params.get("cwd", "")
    context = params.get("context", "")
    store_result = params.get("store_result", "")
    timeout = params.get("timeout", 300)
    constraints = params.get("constraints", {})

    # 解析约束
    verbose = constraints.get("verbose", False) if isinstance(constraints, dict) else False
    output_format = constraints.get("output_format", "text") if isinstance(constraints, dict) else "text"
    max_length = constraints.get("max_length", 0) if isinstance(constraints, dict) else 0

    # 校验 timeout 合法性
    if not isinstance(timeout, (int, float)) or timeout < 10:
        timeout = 10
    elif timeout > 900:
        timeout = 900
    timeout = int(timeout)

    if not task.strip():
        return json.dumps(
            {
                "status": "error",
                "result": "task 参数不能为空",
            },
            ensure_ascii=False,
            indent=2,
        )

    if not cwd.strip():
        return json.dumps(
            {
                "status": "error",
                "result": "cwd 参数不能为空。请用 bash(pwd) 获取当前工作目录后传入。",
            },
            ensure_ascii=False,
            indent=2,
        )
    if not os.path.isdir(cwd):
        return json.dumps(
            {
                "status": "error",
                "result": f"cwd 目录不存在: {cwd}",
            },
            ensure_ascii=False,
            indent=2,
        )

    # ═══════════════════════════════════════════════════════════
    # 防递归守卫：子 agent 不可再次创建子 agent
    # ═══════════════════════════════════════════════════════════
    if os.environ.get("FP_IS_SUBAGENT") == "1":
        return json.dumps(
            {
                "status": "error",
                "result": "递归调用被拒绝：当前进程已是子 agent（FP_IS_SUBAGENT=1），"
                "不可再次创建子 agent。请直接在当前上下文中完成任务。",
            },
            ensure_ascii=False,
            indent=2,
        )

    # ═══════════════════════════════════════════════════════════
    # 构造查询
    # ═══════════════════════════════════════════════════════════
    query = f"[背景信息]\n{context}\n\n[任务]\n{task}" if context else task

    # ═══════════════════════════════════════════════════════════
    # 定位入口 — 通过 python -m fp_cli.main 启动子进程
    # （不依赖文件系统路径，fp-terminal 必须已安装）
    # ═══════════════════════════════════════════════════════════
    entry = [sys.executable, "-m", "fp_cli.main", "-m"]

    # ═══════════════════════════════════════════════════════════
    # 启动子进程（异步）
    # ═══════════════════════════════════════════════════════════
    start_time = time.time()

    env = os.environ.copy()
    env["FP_IS_SUBAGENT"] = "1"
    env["FP_SUBAGENT_QUIET"] = "1"
    if not verbose:
        env["FP_SUBAGENT_SILENT"] = "1"

    try:
        proc = await asyncio.create_subprocess_exec(
            *entry,
            query,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            duration = time.time() - start_time
            return json.dumps(
                {
                    "status": "error",
                    "result": f"子任务超时（{timeout}秒）",
                    "duration": f">{timeout}s",
                    "estimated_tokens": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            proc.kill()
            await proc.wait()
            raise

    except Exception as e:
        duration = time.time() - start_time
        return json.dumps(
            {
                "status": "error",
                "result": f"子任务启动失败: {e}",
                "duration": f"{duration:.1f}s",
                "estimated_tokens": 0,
            },
            ensure_ascii=False,
            indent=2,
        )

    duration = time.time() - start_time
    duration_str = f"{duration:.1f}s"

    # ═══════════════════════════════════════════════════════════
    # 错误处理
    # ═══════════════════════════════════════════════════════════
    output = stdout.decode("utf-8", errors="replace").strip()
    stderr.decode("utf-8", errors="replace").strip()

    # ═══════════════════════════════════════════════════════════
    # 提取子 agent 的实际回复（在 stdout 中寻找 handle_command 的结果）
    # ═══════════════════════════════════════════════════════════

    if not output:
        return json.dumps(
            {
                "status": "warning",
                "result": "子任务完成但无输出文本",
                "duration": duration_str,
                "estimated_tokens": 0,
            },
            ensure_ascii=False,
            indent=2,
        )

    # ═══════════════════════════════════════════════════════════
    # 后处理：按约束转换输出
    # ═══════════════════════════════════════════════════════════

    # 截断
    if max_length and isinstance(max_length, (int, float)) and max_length > 0:
        max_length = int(max_length)
        if len(output) > max_length:
            output = output[:max_length] + f"\n\n...（已截断，原文 {len(output)} 字符）"

    # 格式转换
    if output_format == "json":
        try:
            json.loads(output)
        except (json.JSONDecodeError, ValueError):
            output = json.dumps({"reply": output}, ensure_ascii=False, indent=2)

    # ═══════════════════════════════════════════════════════════
    # 可选：保存到记忆
    # ═══════════════════════════════════════════════════════════
    if store_result:
        try:
            from .memory_save_plugin import execute as memory_save

            await memory_save({
                "name": store_result,
                "type": "reference",
                "description": f"[subagent] {task[:80]}",
                "content": output,
            })
        except Exception as e:
            output += f"\n\n⚠️ 记忆保存失败 ({store_result}): {e}"

    # ═══════════════════════════════════════════════════════════
    # 返回结果：成功时返回纯文本，主 agent 直接看到子 agent 的回复
    # 不包装 JSON 壳，不让 metadata 污染 LLM 的 tool result
    # ═══════════════════════════════════════════════════════════
    return output
