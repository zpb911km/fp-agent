"""
reload 工具 — 两段式热重启（自举激活的唯一通道）

协议（用户红线 reload_gate_protocol，两段式硬约束）：
  阶段一：不带 token 调用 → 拒绝 + 隔离测试告示 + 动态口令（一次性、15 分钟）
  阶段二：携带 token 调用 → 补齐本轮全部缺失 tool 结果并落盘 → 写 handoff →
          execve 按原启动命令重启 → 新实例恢复会话、注入本条 tool 返回并自动续接对话
  exec 失败 → 全部回滚，当前实例继续运行（救火原则：旧进程不死则无损）。

形态说明：标准单工具扩展（PLUGIN_DEFINITION + execute），随 fp-core 内置分发。
State 经 contextvar 导线在执行期读取（fp_core.core.state.get_current_state）——
工具契约是 async def(params)，签名不携带上下文（见 tools/__init__.py 签名约定），
早期为 ON_INIT 注册的生命周期插件形态，方案1重构后升格为内置工具扩展。

跨进程续接契约与新入口三步接入见 fp_core/core/handoff.py。
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import sys
import time
from typing import Any

from fp_core.core.handoff import HANDOFF_TTL, handoff_path
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


# ── 工具执行体 ─────────────────────────────────────────────


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

    # ── 前置检查：启动命令快照 ──
    launch_raw = os.environ.get("FP_LAUNCH_JSON", "")
    if not launch_raw:
        return "❌ 缺少 FP_LAUNCH_JSON（本进程不是经 fp 入口启动），无法重启。请用 `fp` 启动。"
    try:
        launch = json.loads(launch_raw)
        launch_argv: list[str] = list(launch["argv"])
    except (ValueError, KeyError, TypeError):
        return "❌ FP_LAUNCH_JSON 损坏，无法重启。"

    # ── 前置检查：陈旧 handoff 残留 ──
    hp = handoff_path()
    if os.path.isfile(hp):
        try:
            with open(hp, encoding="utf-8") as f:
                old = json.load(f)
            if time.time() - float(old.get("ts", 0)) > HANDOFF_TTL:
                os.remove(hp)  # 陈旧残留：清理后继续
            else:
                return "❌ 存在未消费的 reload handoff（<15 分钟），请先排查再重试。"
        except (OSError, ValueError, TypeError):
            pass

    st = get_current_state()
    if st is None or st.conversation is None or st.session is None:
        return "❌ 未取到活体 State（contextvar 导线未绑定——仅主实例处理链内可用），无法 reload。"

    conv = st.conversation
    sid = st.session.session_id
    frontend = getattr(st.io, "frontend", "unknown")

    # ── 1) 补齐本轮所有缺失的 tool 结果（含 reload 自身）──
    #    保证落盘会话对 API 合法（assistant.tool_calls 逐条有应答）。
    #    同轮并行的其他工具调用标记为"被中断"，续接后模型可见并可重发。
    result_text = (
        f"✅ reload 执行成功：进程已按原入口重启，代码为磁盘最新状态；"
        f"会话 {sid} 已恢复，对话正在自动续接（本条即重启后的新实例所见）。"
    )
    snapshot = [dict(m) for m in conv._messages]  # pyright: ignore[reportPrivateUsage] 回滚快照（含 system，replace_all 为裸替换）
    msgs = conv.to_serializable()
    answered = {m.get("tool_call_id") for m in msgs if m.get("role") == "tool"}
    if msgs and msgs[-1].get("role") == "assistant" and msgs[-1].get("tool_calls"):
        for tc in msgs[-1]["tool_calls"]:
            tid = tc.get("id")
            if not tid or tid in answered:
                continue
            fn = tc.get("function") or {}
            if fn.get("name") == "reload":
                conv.add_tool_message(tid, result_text)
            else:
                conv.add_tool_message(
                    tid,
                    "⏸ 该工具调用在 reload 重启时被中断，未执行（如需要请重新发起）。",
                )

    # ── 2) 落盘（原子覆盖；此刻文件 = 新实例要恢复的完整状态）──
    try:
        st.session.save_context(conv.to_serializable())
    except Exception as e:
        conv.replace_all(snapshot)
        return f"❌ 落盘失败，未重启（当前实例不受影响）: {e}"

    # ── 3) 写 handoff + 环境变量（新实例的续接凭证）──
    try:
        with open(hp, "w", encoding="utf-8") as f:
            json.dump({"sid": sid, "ts": time.time(), "frontend": frontend}, f)
        env = dict(os.environ)
        env["FP_RELOAD_HANDOFF"] = hp
        env["FP_RELOAD_SID"] = sid
    except OSError as e:
        conv.replace_all(snapshot)
        with contextlib.suppress(Exception):
            st.session.save_context(conv.to_serializable())
        return f"❌ handoff 写入失败，未重启（已回滚）: {e}"

    # ── 4) execve 按原启动命令重启（旧进程到此为止，不再返回）──
    #    失败则全部回滚——当前实例继续活着，可修复后重试。
    try:
        cwd = launch.get("cwd")
        if cwd and os.path.isdir(cwd):
            os.chdir(cwd)
        exe = launch_argv[0]
        if os.path.isfile(exe) and os.access(exe, os.X_OK):
            os.execve(exe, launch_argv, env)
        else:
            # 非可执行入口（如 python 脚本路径）：经解释器启动
            os.execve(sys.executable, [sys.executable, exe] + launch_argv[1:], env)
    except OSError as e:
        conv.replace_all(snapshot)
        with contextlib.suppress(Exception):
            st.session.save_context(conv.to_serializable())
        with contextlib.suppress(OSError):
            os.remove(hp)
        return f"❌ exec 重启失败（{e}）。已回滚消息与 handoff，当前实例仍在运行——请修复后重试。"

    # 不可达：exec 成功则进程映像已被替换
    return ""
