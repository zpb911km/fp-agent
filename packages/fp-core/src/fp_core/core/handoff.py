"""reload handoff — 跨进程续接契约（机制层，入口无关）

两种触发面共用本机制与同一激活核心（``perform_exec_reload``）：
  - 工具面 reload（LLM 发起，两段式口令门禁在工具层，见 reload_plugin）
                                                        → ``kind="tool"``
  - 命令面 /reload（人发起，豁免口令）                  → ``kind="command"``

流程（旧实例发起，见用户红线 reload_gate_protocol）：
    旧实例: 落盘会话（tool 面补齐全轮缺失的 tool 结果）→ 写 handoff 文件
            → 设置 FP_RELOAD_HANDOFF/FP_RELOAD_SID 环境变量
            → os.execve 按原启动命令重启；任何一步失败全回滚，当前实例无损
    新实例: 入口构造 Agent 时传 resume=FP_RELOAD_SID 恢复会话
            → Agent 就绪后调 consume_reload_handoff()
              · kind="tool"    → 置 state._pending_continue，返回 True
              · kind="command" → 置 state._reload_notice，返回 False
                                 （命令发生在 process 入口，无挂起工具轮，不进续接）
            → 入口在自己的控制点驱动续接 / 显示完成提示

**新入口接入契约（四步，未来新入口照此实现即自动兼容 reload）：**
  0. ``main()`` 首行 ``capture_launch_command()``——捕获原始启动命令
     （``FP_LAUNCH_JSON``，setdefault 保证重启链恒为首次命令；
     快照缺失时激活核心从 ``/proc/self`` 精确重建，不依赖入口版本）
  1. 构造 Agent：``resume=os.environ.get("FP_RELOAD_SID")``
  2. Agent 就绪（ensure_initialized 后）：``consume_reload_handoff(agent)``
  3. 返回 True → 驱动 ``await agent.continue_conversation(io=本入口IO)``，
     渲染返回内容，完成后把 ``state._pending_continue`` 置 None；
     无论返回值，检查 ``state._reload_notice`` → 显示该行提示并置 None。
  另：ACP 类长连接入口须为 exec 前挂起的请求补发 result（FP_ACP_PENDING_REQ），
      两种 kind 都要补发，否则客户端悬等。

handoff 文件：``{DATA}/reload_handoff.json`` = ``{sid, ts, frontend, kind}``。
消费即删（防同进程二次消费），TTL 兜底（防陈旧 handoff 误触发）。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fp_core.core.agent import Agent
    from fp_core.core.state import State

# handoff 有效期（秒）。reload 是落盘→exec 的即时衔接，超过此值视为陈旧残留。
HANDOFF_TTL = 900


def handoff_path() -> str:
    """handoff 文件的规范路径（工具面/命令面与本模块共用）"""
    from fp_core.platform_utils import get_data_dir

    return os.path.join(get_data_dir(), "reload_handoff.json")


def capture_launch_command() -> None:
    """入口捕获原始启动命令写入 ``FP_LAUNCH_JSON``（新入口契约第 0 步）。

    三个 console 入口（fp / fp-webui / fp-acp）的 ``main()`` 首行各调一次，
    未来新入口照此接入。必须在任何 argv 改写/前置路由之前调用；
    ``setdefault`` 保证重启链上始终是首次启动命令。
    契约：``{"argv": [...], "cwd": "..."}``。
    """
    os.environ.setdefault(
        "FP_LAUNCH_JSON",
        json.dumps({"argv": sys.argv, "cwd": os.getcwd()}),
    )


def perform_exec_reload(state: State, *, kind: str, tool_result_text: str | None = None) -> str:
    """跨进程热重启的激活核心（工具面与命令面共用）。

    成功路径 execve 替换当前进程映像，永不返回；
    失败返回错误文本——已回滚（会话快照还原 + handoff 删除），当前实例无损
    （救火原则：旧进程不死则无损）。两段式口令门禁属于工具层（reload_plugin），
    人触发的命令面豁免。

    Args:
        state: 活体 State（工具面经 contextvar 导线取得，命令面由命令签名注入）。
        kind: "tool" | "command"，写入 handoff 供新实例分流续接行为。
        tool_result_text: kind="tool" 时必填——reload 自身 tool_call 的返回文本，
            随落盘写入会话，新实例续接后模型可见。

    Returns:
        错误文本（已回滚，当前实例仍在运行）。成功则不返回。
    """
    conv = state.conversation
    session = state.session
    if conv is None or session is None:
        return "❌ 无活体会话，无法 reload。"

    # ── 前置检查：启动命令快照 ──
    # 快照缺失（入口早于捕获机制启动的存量实例）→ 从 /proc/self 精确重建：
    # cmdline/cwd 是内核记录的 exec 事实，与快照同源、非猜测；两者皆不可得才拒绝。
    launch_raw = os.environ.get("FP_LAUNCH_JSON", "")
    launch_argv: list[str]
    if launch_raw:
        try:
            launch = json.loads(launch_raw)
            launch_argv = list(launch["argv"])
        except (ValueError, KeyError, TypeError):
            return "❌ FP_LAUNCH_JSON 损坏，无法重启。"
    else:
        try:
            with open("/proc/self/cmdline", "rb") as f:
                launch_argv = [p.decode("utf-8", "surrogateescape") for p in f.read().split(b"\0") if p]
            proc_cwd = os.readlink("/proc/self/cwd")
        except OSError:
            return "❌ 缺少 FP_LAUNCH_JSON 且 /proc/self 不可读，无法重启。"
        if not launch_argv:
            return "❌ 启动命令无法重建（/proc/self/cmdline 为空），无法重启。"
        launch = {"argv": launch_argv, "cwd": proc_cwd}

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

    sid = session.session_id
    frontend = getattr(state.io, "frontend", "unknown")

    # ── 1) 补齐本轮缺失的 tool 结果（仅 tool 面）──
    #    保证落盘会话对 API 合法（assistant.tool_calls 逐条有应答）。
    #    同轮并行的其他工具调用标记为"被中断"，续接后模型可见并可重发。
    #    命令面在 process 入口执行，无挂起工具轮，会话原样落盘。
    snapshot: list[dict[str, Any]] | None = None
    if kind == "tool":
        if tool_result_text is None:
            return "❌ 内部错误：tool 面 reload 未提供 tool_result_text。"
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
                    conv.add_tool_message(tid, tool_result_text)
                else:
                    conv.add_tool_message(
                        tid,
                        "⏸ 该工具调用在 reload 重启时被中断，未执行（如需要请重新发起）。",
                    )

    # ── 2) 落盘（原子覆盖；此刻文件 = 新实例要恢复的完整状态）──
    #    save_context 对空上下文静默跳过、写失败也被其内部吞掉——
    #    故 ensure_on_disk 兜底占位（meta-only 文件让新进程 resume 认得这个 sid）
    #    并做存在性硬校验：文件不在盘上绝不 exec，否则新进程会静默新建会话、
    #    consume 因 sid 不匹配静默放弃续接（E2E 实测踩过）。
    try:
        session.save_context(conv.to_serializable())
    except Exception as e:
        if snapshot is not None:
            conv.replace_all(snapshot)
        return f"❌ 落盘失败，未重启（当前实例不受影响）: {e}"
    if not session.ensure_on_disk():
        if snapshot is not None:
            conv.replace_all(snapshot)
        return "❌ 会话文件未能落盘（磁盘写入失败），未重启——当前实例不受影响。"

    # ── 3) 写 handoff + 环境变量（新实例的续接凭证）──
    try:
        with open(hp, "w", encoding="utf-8") as f:
            json.dump({"sid": sid, "ts": time.time(), "frontend": frontend, "kind": kind}, f)
        env = dict(os.environ)
        env["FP_RELOAD_HANDOFF"] = hp
        env["FP_RELOAD_SID"] = sid
    except OSError as e:
        if snapshot is not None:
            conv.replace_all(snapshot)
            with contextlib.suppress(Exception):
                session.save_context(conv.to_serializable())
        with contextlib.suppress(OSError):
            os.remove(hp)
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
        if snapshot is not None:
            conv.replace_all(snapshot)
            with contextlib.suppress(Exception):
                session.save_context(conv.to_serializable())
        with contextlib.suppress(OSError):
            os.remove(hp)
        return f"❌ exec 重启失败（{e}）。已回滚消息与 handoff，当前实例仍在运行——请修复后重试。"

    return "❌ execve 意外返回（进程未被替换）。请检查 FP_LAUNCH_JSON。"


def consume_reload_handoff(agent: Agent) -> bool:
    """校验并消费 reload handoff，按 kind 分流置位续接状态。

    幂等且一次性：无论成败都会清除环境变量 FP_RELOAD_HANDOFF/FP_RELOAD_SID
    并删除 handoff 文件，保证同一进程不会二次触发续接。

    Returns:
        True = kind="tool" 的有效 handoff（state._pending_continue 已置位，
        入口应驱动 continue_conversation）；
        False = 无 handoff / 已过期 / sid 不匹配（resume 未生效），
        或 kind="command"（此时 state._reload_notice 已置位，入口显示该行即可）。
    """
    path = os.environ.get("FP_RELOAD_HANDOFF", "")
    os.environ.pop("FP_RELOAD_HANDOFF", None)
    os.environ.pop("FP_RELOAD_SID", None)
    if not path:
        return False

    payload: dict[str, Any] | None = None
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        payload = None
    finally:
        # 消费即删：文件是单次通行证
        with contextlib.suppress(OSError):
            os.remove(path)

    if not isinstance(payload, dict):
        return False

    # TTL：exec 是即时衔接，900 秒还没消费说明是陈旧残留
    if time.time() - float(payload.get("ts", 0)) > HANDOFF_TTL:
        return False

    # sid 必须与当前实例恢复到的会话一致——否则 resume 没生效，续接会张冠李戴
    if payload.get("sid") != agent.session.session_id:
        return False

    if payload.get("kind", "tool") == "command":
        # 命令面：会话已由 resume 恢复，无挂起工具轮，不进续接；
        # 置完成提示，由入口在控制点显示一行即回到正常输入。
        agent.state._reload_notice = (  # pyright: ignore[reportPrivateUsage] 设计内跨类协议（state.py 注释明确该字段供 handoff 流程使用）
            f"🔄 热重启完成，会话 {payload['sid']} 已恢复"
        )
        return False

    agent.state._pending_continue = payload  # pyright: ignore[reportPrivateUsage] 设计内跨类协议（state.py 注释明确该字段供 handoff 流程使用）
    return True
