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
        "description": "🚀 派遣子 agent 执行独立任务 —— 这是节省 token 的核心武器！\n\n"
        "为什么用 subagent？\n"
        "1. 子 agent 有自己的独立上下文 → 中间步骤/工具结果/大段文件不占用主上下文\n"
        "2. 同子任务内 prefix 稳定 → API 缓存命中 → 半价\n"
        "3. 子 agent 执行完只返回结论 → 主上下文不膨胀\n\n"
        "⚠️ 经验法则：任何需要 ≥2 次工具调用或读取大文件的任务，都用 subagent！\n"
        "  单步简单操作（1个工具、结果 <200 token）可自己做。\n\n"
        "子 agent 拥有完整工具链（bash/文件读写/搜索/记忆/Python等），"
        "其思考和中间过程完全在独立上下文中完成，不消耗主上下文的 token。\n\n"
        "适合：多步数据分析、文件批量处理、代码编写与调试、信息检索与整理等独立子任务。\n\n"
        "参数说明：\n"
        "- task: 任务描述，清晰完整的一句话或一段话\n"
        "- cwd: 子 agent 的工作目录。所有相对路径基于此目录解析，"
        "应设为父 agent 当前的工作目录（建议用 pwd 命令获取）\n"
        "- context: 背景上下文（可选），如文件路径、数据摘要、关键变量等，"
        "子 agent 没有当前对话历史，请在此提供必要背景\n"
        "- store_result: 记忆键名（可选），若提供则自动将结果存入跨会话记忆\n"
        "- timeout: 超时秒数（可选，默认 300，最小 10，最大 900）\n"
        "- constraints: 输出契约（可选），控制返回内容与格式，默认静默模式只输出结论",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "需要子 agent 执行的具体任务描述。应当清晰、完整、自包含，"
                    "包含所有必要的信息让子 agent 能独立完成任务。",
                },
                "cwd": {
                    "type": "string",
                    "description": "必填。子 agent 的工作目录。所有相对路径基于此目录解析。"
                    "应设为父 agent 当前的工作目录。通过 bash(pwd) 获取。",
                },
                "context": {
                    "type": "string",
                    "description": "传递给子 agent 的背景上下文。可以包含：文件路径、关键代码片段、"
                    "数据摘要、变量值、前置分析结论等。子 agent 没有当前会话历史，"
                    "所有需要的信息都必须通过此参数传递。",
                },
                "store_result": {
                    "type": "string",
                    "description": "可选。若提供，子 agent 的最终回复将自动存入跨会话记忆。"
                    "之后可通过 memory_read 读取。例如：'draft_content', 'analysis_report'",
                },
                "timeout": {
                    "type": "integer",
                    "description": "子任务超时秒数（可选，默认 300，范围 10~900）。"
                    "简单任务（如单次查询）可设为 30~60，复杂任务（如批量文件处理）可设为 300~900。",
                },
                "constraints": {
                    "type": "object",
                    "description": "输出契约。控制 subagent 的返回内容与格式。",
                    "properties": {
                        "verbose": {
                            "type": "boolean",
                            "description": "false（默认）= 静默模式，subagent 只输出最终结论，"
                            "思考链/工具日志均不进入主上下文，token 最省。"
                            "true = 调试模式，输出完整推理过程。",
                        },
                        "output_format": {
                            "type": "string",
                            "enum": ["text", "json", "markdown"],
                            "description": "输出格式。text（默认）= 纯文本，json = 结构化数据，markdown = 格式化文本。",
                        },
                        "max_length": {
                            "type": "integer",
                            "description": "结果最大字符数（可选，默认不限制）。超过则截断并标注。",
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
