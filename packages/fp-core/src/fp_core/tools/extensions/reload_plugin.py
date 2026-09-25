"""
reload 工具 — 两段式热重启（自举激活的唯一通道，工具面/LLM 发起）

协议（用户红线 reload_gate_protocol，两段式硬约束）：
  阶段一：不带 token 调用 → 拒绝 + 隔离测试告示 + 动态口令（一次性、15 分钟）
  阶段二：携带 token 调用 → 转入激活核心 fp_core.core.handoff.perform_exec_reload
          （补齐全轮缺失 tool 结果并落盘 → 写 handoff(kind=tool) →
            execve 按原启动命令重启 → 新实例恢复会话、注入本条 tool 返回并自动续接）
  exec 失败 → 核心内全回滚，当前实例继续运行（救火原则：旧进程不死则无损）。

门禁与激活的切分：两段式口令只约束 LLM（本文件）；激活机制在 core 层，
命令面 /reload（人触发、豁免口令）与本工具共用同一 perform_exec_reload。

形态说明：标准单工具扩展（PLUGIN_DEFINITION + execute），随 fp-core 内置分发。
State 经 contextvar 导线在执行期读取（fp_core.core.state.get_current_state）——
工具契约是 async def(params)，签名不携带上下文（见 tools/__init__.py 签名约定）。

跨进程续接契约与新入口三步接入见 fp_core/core/handoff.py。
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import time
from typing import Any

from fp_core.core.handoff import perform_exec_reload
from fp_core.core.state import get_current_state
from fp_core.platform_utils import get_data_dir

TOKEN_TTL = 900  # 动态口令有效期（秒）：15 分钟，一次性

# ── 插件定义（OpenAI function calling schema） ──────────────────────

PLUGIN_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "reload",
        "description": (
            "进程级热重启（自举激活的唯一通道）。两段式硬协议：不带 token 调用只会"
            "拿到隔离测试告示与动态口令；必须先用子进程完成隔离测试，再携带口令调用"
            "才真正执行重启。重启后本进程被 exec 替换，会话自动恢复并续接对话。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "第一次调用（不带参数）返回的动态口令；省略=只获取告示与口令",
                },
            },
        },
    },
}

NOTICE = """🚫 **reload 拒绝执行：未携带动态口令**（两段式协议，红线不可绕）

**第一步 · 隔离测试（硬性前置）：**
1. 确认全部修改已落盘（git 可回滚）
2. 用 `subagent` 或 bash 拉起**子进程 fp** —— 新代码会被子进程加载
3. 让子进程验证：扩展可导入 / 注册表含新工具 / 新功能冒烟 / 回归集子集
4. 全部通过 → 携带下方口令再次调用 reload；任一失败 → 修复或 git 回滚（当前实例未受影响）

**第二步 · 动态口令（{ttl} 分钟内有效、一次性）：**
`token` = `{token}`

携口令调用后执行：补齐本轮 tool 结果并落盘会话 → 写 handoff →
execve 按原启动命令重启 → 新实例恢复会话、注入本条 tool 返回并自动续接对话。
exec 失败自动回滚，当前实例继续运行。"""


# ── 工具执行体（门禁；激活转 core）──────────────────────────


async def execute(params: dict[str, Any]) -> str:
    token = str(params.get("token") or "").strip()

    # 子进程内禁止 reload：exec 会破坏父子 stdout 回传契约
    if os.environ.get("FP_IS_SUBAGENT") == "1":
        return "🚫 子 agent 内禁止 reload（exec 会破坏父子进程契约）。请在主实例执行。"

    data_dir = get_data_dir()
    token_path = os.path.join(data_dir, "reload.token")

    # ── 阶段一：无口令 → 拒绝 + 告示 + 发口令 ──
    if not token:
        tok = secrets.token_hex(6)
        os.makedirs(data_dir, exist_ok=True)
        with open(token_path, "w", encoding="utf-8") as f:
            json.dump({"token": tok, "ts": time.time()}, f)
        return NOTICE.format(ttl=TOKEN_TTL // 60, token=tok)

    # ── 阶段二：校验口令（读取即销毁，单次有效）──
    rec: dict[str, Any] | None = None
    try:
        with open(token_path, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, json.JSONDecodeError):
        rec = None
    with contextlib.suppress(OSError):
        os.remove(token_path)
    if not rec or rec.get("token") != token:
        return "❌ 口令无效或已使用。重新调用 reload（不带参数）获取新口令。"
    if time.time() - float(rec.get("ts", 0)) > TOKEN_TTL:
        return "❌ 口令已过期（15 分钟）。重新调用 reload（不带参数）获取新口令。"

    st = get_current_state()
    if st is None or st.conversation is None or st.session is None:
        return "❌ 未取到活体 State（contextvar 导线未绑定——仅主实例处理链内可用），无法 reload。"

    result_text = (
        f"✅ reload 执行成功：进程已按原入口重启，代码为磁盘最新状态；"
        f"会话 {st.session.session_id} 已恢复，对话正在自动续接（本条即重启后的新实例所见）。"
    )
    # 激活核心：落盘 → handoff(kind=tool) → execve；失败返回错误文本（已回滚），
    # 成功永不返回。启动命令/陈旧 handoff 等前置检查亦在核心内。
    return await perform_exec_reload(st, kind="tool", tool_result_text=result_text)
