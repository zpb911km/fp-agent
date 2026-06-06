"""
Subagent 插件 — 派遣子 agent 执行独立任务

主 agent 可以派遣子 agent 处理独立子任务，子 agent 拥有独立的会话上下文，
其全部思考过程和工具调用都不占用主上下文的 token 空间。
执行结束后返回结果摘要。

架构哲学：
  - 主 agent = 指挥官（分解问题、分配任务、合成结果）
  - 子 agent = 士兵（专注执行单一子任务）
  - 记忆（memory）是共享的白板，主/子 agent 均可读写

防递归：子 agent 不可再次创建子 agent（环境变量守卫 + 执行时拒绝）
"""

import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, Optional


# ── 插件定义（OpenAI function calling schema） ──────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "subagent",
        "description": "派遣一个子 agent 执行独立任务。子 agent 拥有完整工具链（bash/文件读写/搜索/记忆/Python等），"
                       "但其思考和中间过程完全在独立上下文中完成，不占用当前对话的 token。"
                       "适合：多步数据分析、文件批量处理、代码编写与调试、信息检索与整理等独立子任务。\n\n"
                       "参数说明：\n"
                       "- task: 任务描述，清晰完整的一句话或一段话\n"
                       "- context: 背景上下文（可选），如文件路径、数据摘要、关键变量等，"
                       "子 agent 没有当前对话历史，请在此提供必要背景\n"
                       "- store_result: 记忆键名（可选），若提供则自动将结果存入跨会话记忆\n"
                       "- timeout: 超时秒数（可选，默认 300，最小 10，最大 600）\n"
                       "- constraints: 输出契约（可选），控制返回内容与格式，默认静默模式只输出结论",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "需要子 agent 执行的具体任务描述。应当清晰、完整、自包含，"
                                   "包含所有必要的信息让子 agent 能独立完成任务。"
                },
                "context": {
                    "type": "string",
                    "description": "传递给子 agent 的背景上下文。可以包含：文件路径、关键代码片段、"
                                   "数据摘要、变量值、前置分析结论等。子 agent 没有当前会话历史，"
                                   "所有需要的信息都必须通过此参数传递。"
                },
                "store_result": {
                    "type": "string",
                    "description": "可选。若提供，子 agent 的最终回复将自动存入跨会话记忆。"
                                   "之后可通过 memory_read 读取。例如：'draft_content', 'analysis_report'"
                },
                "timeout": {
                    "type": "integer",
                    "description": "子任务超时秒数（可选，默认 300，范围 10~600）。"
                                   "简单任务（如单次查询）可设为 30~60，复杂任务（如批量文件处理）可设为 300~600。"
                },
                "constraints": {
                    "type": "object",
                    "description": "输出契约。控制 subagent 的返回内容与格式。",
                    "properties": {
                        "verbose": {
                            "type": "boolean",
                            "description": "false（默认）= 静默模式，subagent 只输出最终结论，"
                                           "思考链/工具日志均不进入主上下文，token 最省。"
                                           "true = 调试模式，输出完整推理过程。"
                        },
                        "output_format": {
                            "type": "string",
                            "enum": ["text", "json", "markdown"],
                            "description": "输出格式。text（默认）= 纯文本，json = 结构化数据，markdown = 格式化文本。"
                        },
                        "max_length": {
                            "type": "integer",
                            "description": "结果最大字符数（可选，默认不限制）。超过则截断并标注。"
                        }
                    }
                }
            },
            "required": ["task"]
        }
    }
}


def execute(params: Dict[str, Any]) -> str:
    """
    执行子 agent 任务
    
    Args:
        params: 包含 task, context, store_result, timeout, constraints 的字典
        
    Returns:
        JSON 格式的执行结果
    """
    task = params.get("task", "")
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
    elif timeout > 600:
        timeout = 600
    timeout = int(timeout)

    if not task.strip():
        return json.dumps({
            "status": "error",
            "result": "task 参数不能为空",
        }, ensure_ascii=False, indent=2)

    # ═══════════════════════════════════════════════════════════
    # 防递归守卫：子 agent 不可再次创建子 agent
    # ═══════════════════════════════════════════════════════════
    if os.environ.get("FP_IS_SUBAGENT") == "1":
        return json.dumps({
            "status": "error",
            "result": "递归调用被拒绝：当前进程已是子 agent（FP_IS_SUBAGENT=1），"
                      "不可再次创建子 agent。请直接在当前上下文中完成任务。",
        }, ensure_ascii=False, indent=2)

    # ═══════════════════════════════════════════════════════════
    # 构造查询
    # ═══════════════════════════════════════════════════════════
    if context:
        query = f"[背景信息]\n{context}\n\n[任务]\n{task}"
    else:
        query = task

    # ═══════════════════════════════════════════════════════════
    # 定位入口脚本（相对于本文件的路径）
    #   tools/plugins/subagent_plugin.py → tools/ → agent_v2 root
    # ═══════════════════════════════════════════════════════════
    agent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cli_py = os.path.join(agent_dir, "cli.py")

    if not os.path.exists(cli_py):
        return json.dumps({
            "status": "error",
            "result": f"找不到 cli.py（预期：{cli_py}）",
        }, ensure_ascii=False, indent=2)

    # ═══════════════════════════════════════════════════════════
    # 启动子进程
    # ═══════════════════════════════════════════════════════════
    start_time = time.time()

    env = os.environ.copy()
    env["FP_IS_SUBAGENT"] = "1"     # 标记为子 agent（防递归）
    env["FP_SUBAGENT_QUIET"] = "1"  # 安静模式（不打印 logo 等）
    if not verbose:
        env["FP_SUBAGENT_SILENT"] = "1"  # 静默模式：LLM 流式/工具日志/迭代统计全部隐藏

    try:
        result = subprocess.run(
            [sys.executable, cli_py, "-m", query],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=agent_dir,
        )
    except subprocess.TimeoutExpired:
        duration = time.time() - start_time
        return json.dumps({
            "status": "error",
            "result": f"子任务超时（{timeout}秒）",
            "duration": f">{timeout}s",
            "estimated_tokens": 0,
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        duration = time.time() - start_time
        return json.dumps({
            "status": "error",
            "result": f"子任务启动失败: {e}",
            "duration": f"{duration:.1f}s",
            "estimated_tokens": 0,
        }, ensure_ascii=False, indent=2)

    duration = time.time() - start_time
    duration_str = f"{duration:.1f}s"

    # ═══════════════════════════════════════════════════════════
    # 错误处理
    # ═══════════════════════════════════════════════════════════
    if result.returncode != 0:
        error_detail = result.stderr.strip()[:500] if result.stderr.strip() else "无错误信息"
        return json.dumps({
            "status": "error",
            "result": f"子进程异常退出（退出码 {result.returncode}）: {error_detail}",
            "duration": duration_str,
            "estimated_tokens": 0,
        }, ensure_ascii=False, indent=2)

    # ═══════════════════════════════════════════════════════════
    # 提取输出
    # ═══════════════════════════════════════════════════════════
    output = result.stdout.strip()

    if not output:
        return json.dumps({
            "status": "warning",
            "result": "子任务完成但无输出文本",
            "duration": duration_str,
            "estimated_tokens": 0,
        }, ensure_ascii=False, indent=2)

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
            # 如果输出本身已是 JSON，不做额外包装
            json.loads(output)
        except (json.JSONDecodeError, ValueError):
            output = json.dumps({"reply": output}, ensure_ascii=False, indent=2)

    # 估算 token 数（粗略）
    estimated_tokens = len(output) // 3

    # ═══════════════════════════════════════════════════════════
    # 可选：保存到记忆
    # ═══════════════════════════════════════════════════════════
    if store_result:
        try:
            # 直接调用 memory_save 插件来保存
            from .memory_save_plugin import execute as memory_save
            memory_save({
                "name": store_result,
                "type": "reference",
                "description": f"[subagent] {task[:80]}",
                "content": output,
            })
        except Exception as e:
            output += f"\n\n⚠️ 记忆保存失败 ({store_result}): {e}"

    # ═══════════════════════════════════════════════════════════
    # 返回结果（JSON 格式，便于主 agent 解析）
    # ═══════════════════════════════════════════════════════════
    mode_note = "subagent 静默模式" if not verbose else "subagent 调试模式（含完整过程）"
    summary = {
        "status": "success",
        "result": output,
        "duration": duration_str,
        "estimated_tokens": estimated_tokens,
        "mode": mode_note,
        "note": "子 agent 的推理过程在独立会话中完成，未占用主上下文。",
    }

    return json.dumps(summary, ensure_ascii=False, indent=2)
