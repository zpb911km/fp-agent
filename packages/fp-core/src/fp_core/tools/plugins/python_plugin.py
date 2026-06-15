"""
Python 插件 — 执行 Python 代码（异步版本）

通过临时文件方式执行用户提供的 Python 代码，适合复杂数据处理和算法验证。
"""

import asyncio
import contextlib
import os
import sys
import tempfile
from typing import Any

# ── 插件定义（OpenAI function calling schema） ──────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "python",
        "description": "执行一段 Python 代码（通过临时文件），适合复杂数据处理、算法验证等。超时 30 秒。",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "要执行的 Python 代码"},
            },
            "required": ["code"],
        },
    },
}


async def execute(params: dict[str, Any]) -> str:
    """
    执行 Python 代码（异步）

    Args:
        params: 包含 'code' 键的字典

    Returns:
        代码执行输出
    """
    code = params.get("code", "")
    if not code:
        raise ValueError("python 插件需要 code 参数")

    tmp_path = None
    try:
        # 写入临时文件（同步文件 IO，用 run_in_executor）
        loop = asyncio.get_running_loop()

        def _write_temp():
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".py",
                prefix="agent_python_",
                delete=False,
                encoding="utf-8",
            ) as f:
                f.write(code)
                path = f.name
            return path

        tmp_path = await loop.run_in_executor(None, _write_temp)

        # 异步执行（强制 UTF-8 编码，防止 GBK/cp936 等 locale 解码失败）
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=30,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return "错误：代码执行超时（30秒）"
        except (KeyboardInterrupt, asyncio.CancelledError):
            proc.kill()
            await proc.wait()
            raise

        output = stdout.decode("utf-8", errors="replace")
        if stderr:
            stderr_text = stderr.decode("utf-8", errors="replace")
            if stderr_text.strip():
                if output:
                    output += "\n"
                output += stderr_text

        # 截断
        if len(output) > 5000:
            output = output[:5000] + f"\n...（已截断，原文 {len(output)} 字符）"

        return output if output else "（无输出）"

    except Exception as e:
        return f"错误：{e}"
    finally:
        # 清理临时文件
        if tmp_path and os.path.exists(tmp_path):
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
