"""
Agent 主干类（全异步版本）
生命周期驱动的完整 Agent 实现
"""

import asyncio
import json
import os
import re
import sys
from collections import deque
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.lifecycle import LifecycleManager, LifecycleHook, HookContext

# ── 跨平台中断标志 ─────────────────────────────────────
# 由信号处理器设置（无平台依赖性），_check_interrupted() 检查。
# 
# 设计原因：
# - Unix: signal handler 在主线程运行，可直接 all_tasks().cancel()
# - Windows: Ctrl+C 在处理线程运行，all_tasks() 可能找不到 event loop
# - 此标志作为跨平台回退，两种平台都能可靠工作
_interrupted_flag = False
from plugins.base.plugin import Plugin, PluginRegistry, PluginConfig

import config
import tools
import display
from core import session
from prompts.agent import load_agent_prompt
from skills.loader import skill_loader
from commands import execute as execute_command, get_all_commands
from core.io import IOChannel, CLIIO


@dataclass
class Message:
    """消息对象"""
    role: str = "user"
    content: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Response:
    """响应对象"""
    content: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


class Agent:
    """
    完整 Agent 实现（全异步）
    
    特性：
    - 生命周期驱动的插件系统
    - 会话管理（持久化）
    - 技能系统（热重载）
    - 工具调用（循环执行）
    - 死循环检测
    - 上下文压缩
    """
    
    def __init__(self, enable_log: bool = False, resume: Optional[str] = None,
                 io: Optional[IOChannel] = None):
        self.enable_log = enable_log
        
        # IO 通道（默认 CLI）
        self.io = io or CLIIO()
        
        # 检查配置
        if not config.check_llm_config():
            raise ValueError("LLM API 配置不完整")
        
        # LLM 客户端（异步 httpx）
        from core.llm_client import Client
        self.client = Client(
            api_key=config.LLM_API_KEY,
            base_url=config.LLM_API_BASE_URL
        )
        self.model = config.LLM_MODEL
        
        # 生命周期管理器
        self.lifecycle = LifecycleManager(enable_log=enable_log)
        
        # 插件注册表（自动扫描 plugins/ 目录, 文件即开关）
        _plugin_dir = os.path.join(os.path.dirname(__file__), '..', 'plugins')
        self.plugins = PluginRegistry(
            self.lifecycle,
            plugin_dir=os.path.normpath(_plugin_dir),
        )
        
        # 技能系统
        skill_loader.load_all()
        self._update_system_prompt()
        
        # 会话管理
        self.session = session.SessionManager(resume=resume)
        os.makedirs(config.SESSIONS_DIR, exist_ok=True)
        
        if not os.environ.get("FP_SUBAGENT_QUIET"):
            display.info(f"📂 新会话：{self.session.session_id}")
        
        # 上下文
        self._context: List[Dict] = []
        
        # 核弹退出标记（由 exit! 命令设置，shutdown 检查）
        self._nuclear_exit = False
        
        # ESC/Ctrl+C 中断标记
        self._interrupted = False
        self._processing = False  # 是否正在处理请求（供信号处理器判断）
        
        # 循环检测器
        self._loop_detector = session.LoopDetector(
            max_iterations=config.MAX_ITERATIONS
        )
        
        # 内置钩子
        self._register_builtin_hooks()
        
        # 初始化上下文
        self._context = self._build_context()
    
    def _register_builtin_hooks(self):
        """注册内置钩子"""
        self.lifecycle.register(
            LifecycleHook.ON_INIT,
            self._on_init,
            priority=0,
            name="builtin_init"
        )
        self.lifecycle.register(
            LifecycleHook.ON_SHUTDOWN,
            self._on_shutdown,
            priority=999,
            name="builtin_shutdown"
        )
    
    async def _on_init(self, ctx: HookContext, **kwargs) -> HookContext:
        """初始化钩子"""
        if self.enable_log:
            print("[Agent] Initializing...")
        ctx.data["initialized"] = True
        return ctx
    
    async def _on_shutdown(self, ctx: HookContext, **kwargs) -> HookContext:
        """关闭钩子"""
        # 保存上下文
        self.session.save_context(self._context)
        if self.enable_log:
            print("[Agent] Shutting down...")
        return ctx
    
    # ============ 技能系统 ============
    
    def _load_system_prompt(self) -> str:
        """加载系统提示词"""
        parts = []
        
        # 确保技能已加载
        if not skill_loader.skills:
            skill_loader.load_all()
        
        # 加载基础提示词
        agent_prompt = load_agent_prompt()
        if agent_prompt:
            parts.append(agent_prompt)
        
        # 加载技能提示词
        skill_text = skill_loader.get_all_prompt_text()
        if skill_text:
            parts.append(skill_text)
        
        # 加入当前时间、路径等状态信息
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current_path = os.getcwd()
        try:
            current_user = os.getlogin()
        except:
            current_user = "unknown"
        
        state_info = f"""
## 当前时间,路径等状态信息
当前时间: {current_time}
当前路径: {current_path}
当前用户: {current_user}
"""
        parts.append(state_info)
        
        return "\n\n".join(parts)
    
    def _update_system_prompt(self):
        """更新系统提示词缓存"""
        self._system_prompt = self._load_system_prompt()
    
    def _build_context(self) -> List[Dict]:
        """构建上下文"""
        system = self._load_system_prompt()
        context = [{"role": "system", "content": system}]
        
        # 从会话文件恢复历史
        context.extend(self.session.load_context(system)[1:])  # 跳过重复的 system
        
        return context
    
    def reload_skills(self) -> bool:
        """热重载技能"""
        try:
            display.info("🔄 正在重新加载技能...")
            skill_loader.reload()
            self._update_system_prompt()
            
            loaded_count = len(skill_loader.skills)
            display.info(f"✅ 技能重载完成！当前可用技能数：{loaded_count}")
            
            for name, skill in sorted(skill_loader.skills.items()):
                title = skill.title if hasattr(skill, 'title') else name
                display.item(f"   • {title} ({name})")
            
            return True
        except Exception as e:
            display.error(f"❌ 技能重载失败：{e}")
            return False
    
    def _cmd_add_skill(self, filename: str):
        """从文件添加新技能"""
        if not filename:
            display.info("❌ 用法：/add_skill <filename>")
            display.item("   示例：/add_skill my_new_skill.md")
            display.info("\n技能文件格式要求:")
            display.item("  1. 文件必须位于 skills/ 目录")
            display.item("  2. 使用 YAML frontmatter 格式:")
            display.item("     ---")
            display.item("     name: skill_name")
            display.item("     title: 技能标题")
            display.item("     description: 技能描述")
            display.item("     category: 分类 (可选)")
            display.item("     version: 版本号 (可选)")
            display.item("     priority: 优先级 1-10 (可选)")
            display.item("     ---")
            display.item("     # 技能正文内容")
            return
        
        if not filename.endswith('.md'):
            display.error(f"❌ 错误：技能文件必须是 .md 格式")
            return
        
        skill_file = os.path.join(config.SKILLS_DIR, filename)
        
        if not os.path.exists(skill_file):
            display.error(f"❌ 错误：文件不存在 {skill_file}")
            return
        
        try:
            # 加载并验证技能
            skill = skill_loader._load_skill_file(skill_file)
            
            if skill is None:
                display.error(f"❌ 错误：无法加载技能文件 {skill_file}")
                return
            
            # 检查是否已存在同名技能
            if skill.name in skill_loader.skills:
                display.warning(f"⚠️ 警告：技能 '{skill.name}' 已存在！")
                display.hint(f"   如需更新，请先删除旧版本或使用 /reload_skills")
                return
            
            # 添加到内存
            skill_loader.skills[skill.name] = skill
            
            # 更新系统提示词
            self._update_system_prompt()
            self._context.clear()
            self._context.extend(self._build_context())
            
            display.info(f"✅ 技能添加成功!")
            display.item(f"   名称：{skill.title} ({skill.name})")
            display.item(f"   版本：v{skill.version}")
            display.item(f"   类别：{skill.category}")
            display.item(f"   优先级：{skill.priority}")
            display.item(f"   文件：{skill_file}")
            display.hint(f"\n🔄 系统提示词已更新，新技能立即可用！")
            
        except Exception as e:
            display.error(f"❌ 添加技能失败：{e}")
            import traceback
            traceback.print_exc()
    
    def _cmd_remove_skill(self, skill_name: str):
        """删除指定技能"""
        if not skill_name:
            display.info("❌ 用法：/remove_skill <name>")
            display.item("   示例：/remove_skill file_manager")
            display.info("\n当前可用技能列表:")
            if skill_loader.skills:
                for name in sorted(skill_loader.skills.keys()):
                    display.item(f"   • {name}")
            else:
                display.info("   暂无可用技能")
            return
        
        # 检查技能是否存在
        if skill_name not in skill_loader.skills:
            display.error(f"❌ 错误：技能 '{skill_name}' 不存在")
            display.info("\n当前可用技能列表:")
            if skill_loader.skills:
                for name in sorted(skill_loader.skills.keys()):
                    display.item(f"   • {name}")
            else:
                display.info("   暂无可用技能")
            return
        
        # 从内存中移除
        del skill_loader.skills[skill_name]
        
        # 更新系统提示词
        self._update_system_prompt()
        self._context.clear()
        self._context.extend(self._build_context())
        
        display.info(f"✅ 技能已删除：{skill_name}")
        display.hint(f"🔄 系统提示词已更新，技能已移除！")
    
    def list_skills(self) -> str:
        """列出所有技能"""
        if not skill_loader.skills:
            skill_loader.load_all()
        
        lines = ["📚 当前可用技能:", ""]
        for name, skill in sorted(skill_loader.skills.items()):
            title = skill.title
            desc = skill.description[:60] + "..." if len(skill.description) > 60 else skill.description
            lines.append(f"  • {title} ({name})")
            lines.append(f"    {desc}")
            lines.append("")
        
        return "\n".join(lines)
    
    # ============ 会话管理 ============
    
    def list_sessions(self) -> str:
        """列出所有会话"""
        sessions = self.session.list_sessions()
        if not sessions:
            return "暂无历史会话"
        
        lines = ["📋 历史会话:", ""]
        current = self.session.session_id
        
        for sid in sorted(sessions.keys(), reverse=True):
            info = sessions[sid]
            marker = " ← 当前" if sid == current else ""
            cnt = info.get("message_count", 0)
            created = info.get("created", "?")
            lines.append(f"  {sid}{marker}  ({cnt}条, {created})")
        
        return "\n".join(lines)
    
    def switch_session(self, sid: str) -> bool:
        """切换会话"""
        if self.session.switch_session(sid):
            self._context = self._build_context()
            return True
        return False
    
    def clear_session(self):
        """清空当前会话"""
        self._context.clear()
        self._context.extend([{"role": "system", "content": self._system_prompt}])
        self.session.clear_context(self._system_prompt)
    
    def delete_session(self, sid: str) -> bool:
        """删除指定会话（不能是当前会话）。返回是否成功。"""
        return self.session.delete_session(sid)
    
    async def _summarize_session_bg(self, old_sid: str, old_context: List[Dict]):
        """后台异步任务：为已保存的旧会话用 LLM 生成摘要并更新 meta。
        注意：此方法在后台 fire-and-forget，不影响主流程。"""
        try:
            history_msgs = [m for m in old_context if m["role"] != "system"]
            if len(history_msgs) < 2:
                return  # 消息太少，不值得总结
            
            summary_msgs = old_context + [
                {"role": "user", "content": "请总结一下，给这次对话起一个5到10个汉字的名字。不要添加任何多余的文字。"}
            ]
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=summary_msgs,
                tools=tools.TOOL_DEFINITIONS,
                stream=False,
                temperature=0.3,
                max_tokens=32,
                extra_body={"enable_thinking": False},
            )
            summary = response.choices[0].message.content or ""
            summary = summary.strip().strip('"').strip("'").strip('「」『』')
            
            if not summary or len(summary) > 50:
                # 回退：取首条用户消息
                for m in history_msgs:
                    if m["role"] == "user":
                        text = m.get("content", "").strip()
                        if text:
                            summary = text.split("\n")[0].strip()[:50]
                            break
            if not summary:
                summary = "empty_session"
            
            # 更新旧会话的 meta
            self.session.update_meta(old_sid, summary=summary)
        except Exception:
            pass  # 后台任务，静默容错
    
    # ============ 中断机制 ============
    
    def cancel(self):
        """信号处理器回调：标记中断，下次检查点会响应"""
        self._interrupted = True
    
    def _check_interrupted(self):
        """检查中断标志并抛出 CancelledError
        
        检查两个来源：
        1. 全局 _interrupted_flag — 由 signal.signal 处理器设置（跨平台安全）
        2. self._interrupted — 由 agent.cancel() 设置（add_signal_handler 回调）
        """
        global _interrupted_flag
        if _interrupted_flag:
            _interrupted_flag = False
            self._interrupted = False
            self._processing = False
            raise asyncio.CancelledError("用户中断")
        if self._interrupted:
            self._interrupted = False
            self._processing = False
            raise asyncio.CancelledError("用户中断")
    
    # ============ LLM 调用（异步） ============
    
    async def _stream_chat(self, context: List[Dict], silent: bool = False) -> Dict:
        """发起聊天请求（非流式，从完整 response 直接提取内容）
        
        非 silent 模式：
          - 发起请求前启动 spinner 动画（避免假死感）
          - 不再打印思考内容（reasoning_content）
          - 收到完整回复后停止 spinner 并立即输出
        """
        # 非 silent 模式：启动 spinner
        spinner = None
        if not silent:
            spinner = display.Spinner("思考中")
            await spinner.start()
        
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=context,
                tools=tools.TOOL_DEFINITIONS,
                stream=False,
                temperature=config.LLM_TEMPERATURE,
                max_tokens=config.LLM_MAX_TOKENS,
                extra_body={"enable_thinking": False},
            )
        finally:
            if spinner:
                await spinner.stop()
        
        message = response.choices[0].message
        
        # 提取回复内容（不输出 reasoning_content）
        reply_content = message.content or ""
        
        # 有实际回复内容时用 streamer 展示（纯文本/工具调用标记）
        streamer = display.LLMStreamer(silent=silent)
        if reply_content:
            streamer.write(reply_content)
        streamer.end()
        
        # 构建返回消息（不再包含 reasoning_content）
        msg = {"role": "assistant", "content": reply_content}
        if message.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in message.tool_calls
            ]
        msg["_interrupted"] = False
        
        return msg
    # ============ 工具执行（异步） ============
    
    async def _execute_tool(self, tc: Dict, silent: bool = False) -> str:
        """执行工具调用（异步）"""
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"])
        except json.JSONDecodeError as e:
            return f"错误：工具参数 JSON 解析失败 - {e}"
        
        if not silent:
            safe_args = {k: str(v) for k, v in args.items()}
            display.llm_tool(f"  🛠️  {name}({json.dumps(safe_args, ensure_ascii=False)})")
        
        try:
            result = await tools.dispatch(name, **args)
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as e:
            return f"❌ 工具执行失败 ({name}): {e}"
        
        if not silent:
            display.llm_tool(f"  📋  {result.strip()}")
        
        return result
    
    # ============ 上下文管理 ============
    
    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """估算 token 数量"""
        return len(text) // 3
    
    def _trim_context(self, context: List[Dict]):
        """超出 token 限制时裁剪上下文"""
        if len(context) <= 1:
            return context
        
        system = context[0]
        rest = context[1:]
        total = sum(self._estimate_tokens(m.get("content", "")) for m in rest)
        
        while total > config.MAX_CONTEXT_TOKENS and len(rest) > 2:
            removed = rest.pop(0)
            total -= self._estimate_tokens(removed.get("content", ""))
            
            if rest and rest[0].get("role") == "assistant":
                assistant = rest.pop(0)
                total -= self._estimate_tokens(assistant.get("content", ""))
                
                if assistant.get("tool_calls"):
                    tool_ids = {tc["id"] for tc in assistant["tool_calls"] if tc.get("id")}
                    while rest and rest[0].get("role") == "tool" and rest[0].get("tool_call_id") in tool_ids:
                        removed_tool = rest.pop(0)
                        total -= self._estimate_tokens(removed_tool.get("content", ""))
                        tool_ids.discard(removed_tool.get("tool_call_id"))
        
        return [system] + rest
    
    @staticmethod
    def _repair_tool_ordering(messages: List[Dict]) -> List[Dict]:
        """修复 tool 消息顺序 — 不丢弃信息，转为合成消息保留"""
        result = []
        buffer: List[Dict] = []
        active_tool_ids: set = set()
        converted = 0
        repaired_assistants = 0
        
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                tc_ids = {tc["id"] for tc in m["tool_calls"] if tc.get("id")}
                missing_results = False
                
                if buffer:
                    result.extend(buffer)
                    buffer = []
                
                active_tool_ids = tc_ids
                result.append(m)
            
            elif m["role"] == "tool":
                tid = m.get("tool_call_id")
                if active_tool_ids and tid and tid in active_tool_ids:
                    active_tool_ids.discard(tid)
                    result.append(m)
                    if not active_tool_ids and buffer:
                        result.extend(buffer)
                        buffer = []
                else:
                    converted += 1
                    tool_content = m.get("content", "")
                    converted_msg = {
                        "role": "user",
                        "content": f"[工具调用结果]:\n{tool_content}"
                    }
                    buffer.append(converted_msg)
            
            else:
                buffer.append(m)
        
        if active_tool_ids:
            for i in range(len(result) - 1, -1, -1):
                if result[i]["role"] == "assistant" and result[i].get("tool_calls"):
                    remaining_ids = {tc["id"] for tc in result[i]["tool_calls"] if tc.get("id")}
                    still_missing = remaining_ids & active_tool_ids
                    if still_missing:
                        tc_texts = []
                        new_tc_list = []
                        for tc in result[i]["tool_calls"]:
                            if tc.get("id") in still_missing:
                                fn = tc.get("function", {})
                                fn_name = fn.get("name", "未知工具")
                                fn_args = fn.get("arguments", "{}")
                                tc_texts.append(f"[工具调用: {fn_name}({fn_args[:100]}) — 结果已丢失]")
                            else:
                                new_tc_list.append(tc)
                        
                        if tc_texts:
                            original_content = result[i].get("content", "")
                            suffix = "\n\n" + "\n".join(tc_texts) if tc_texts else ""
                            result[i]["content"] = original_content + suffix
                            result[i]["tool_calls"] = new_tc_list if new_tc_list else result[i].get("tool_calls", [])
                            if not result[i]["tool_calls"]:
                                del result[i]["tool_calls"]
                            repaired_assistants += 1
                        
                        active_tool_ids -= still_missing
        
        if buffer:
            result.extend(buffer)
        
        if converted:
            display.warning(f"  🔄 已将 {converted} 条孤立的 tool 消息转为 user 消息保留")
        if repaired_assistants:
            display.warning(f"  🔄 已修复 {repaired_assistants} 条缺失 tool result 的 assistant 消息")
        
        return result
    
    async def _compact_context(self):
        """压缩上下文（异步）"""
        history_msgs = [m for m in self._context if m["role"] != "system"]
        
        if len(history_msgs) <= 4:
            display.info("对话历史较短，无需压缩")
            return
        
        recent = history_msgs[-4:]
        to_compact = history_msgs[:-4]
        
        # 格式化待压缩消息
        compact_text = ""
        for m in to_compact:
            role = "用户" if m["role"] == "user" else "AI" if m["role"] == "assistant" else "工具"
            content = m.get("content", "")[:300]
            compact_text += f"[{role}]: {content}\n\n"
        
        # 请求 LLM 压缩
        display.info("🔄 正在压缩对话历史...")
        try:
            compact_context = [
                {"role": "system", "content": "你是一个对话压缩助手，擅长提炼关键信息。"},
                {"role": "user", "content": f"请将以下对话历史压缩为一段连贯的摘要，保留关键信息（用户需求、已做的操作、重要结论）。用中文，200字以内。只输出摘要。\n\n{compact_text}"}
            ]
            assistant_msg = await self._stream_chat(compact_context, silent=True)
            summary = assistant_msg.get("content", "").strip()
            if not summary:
                raise ValueError("LLM 返回空")
        except Exception as e:
            display.error(f" 压缩失败: {e}")
            return
        
        # 重建上下文
        system = self._context[0]
        self._context = [system]
        self._context.append({"role": "system", "content": f"以下是压缩后的对话历史摘要（省略了 {len(to_compact)} 条早期消息）：\n{summary}"})
        self._context.extend(recent)
        
        display.info(f" ✅\n📦 已压缩 {len(to_compact)} 条消息为摘要")
    
    async def _cmd_back(self, target_idx: int = None, mode: int = None) -> str:
        """
        回退到对话的某个历史时刻（异步）
        
        参数：
            target_idx: 目标消息序号（1-based，从第一条非 system 消息开始）
                        为 None 时进入交互模式
            mode:       1=保留后续消息，2=删除后续消息（默认）
        
        返回状态信息（用于 Response.content）
        """
        history_msgs = [m for m in self._context if m["role"] != "system"]
        
        if not history_msgs:
            msg = "没有历史记录可以回退"
            self.io.info(msg)
            return msg
        
        # ── 有参数：直接回溯 ──
        if target_idx is not None:
            if target_idx < 1 or target_idx > len(history_msgs):
                msg = f"❌ 无效索引：{target_idx}，有效范围 1~{len(history_msgs)}"
                self.io.error(msg)
                return msg
            
            idx_in_history = target_idx - 1  # 转为 0-based
            sys_count = sum(1 for m in self._context if m["role"] == "system")
            
            if mode is None or mode == 2:
                # 删除后续消息
                del self._context[sys_count + idx_in_history + 1:]
                self.session.save_context(self._context)
                msg = f"⏪ 已回退到第 {target_idx} 条消息，后续消息已删除"
                self.io.info(msg)
                return msg
            else:  # mode == 1，保留后续消息
                self.session.save_context(self._context)
                msg = f"⏪ 已回退到第 {target_idx} 条消息，后续消息已保留"
                self.io.info(msg)
                return msg
        
        # ── 无参数：交互模式（原有逻辑） ──
        roles_zh = {"user": "👤 用户", "assistant": "🤖 AI", "tool": "🔧 工具"}
        lines = [f"📜 对话历史（共 {len(history_msgs)} 条消息）:"]
        
        for i, msg in enumerate(history_msgs):
            role = roles_zh.get(msg["role"], msg["role"])
            content = msg.get("content", "")
            if msg["role"] == "tool":
                content = content[:60] + "..." if len(content) > 60 else content
            else:
                content = content[:120] + "..." if len(content) > 120 else content
            content = content.replace("\n", " ")
            lines.append(f"  [{i+1:3d}] {role}: {content}")
        
        prompt_hint = f"输入 1~{len(history_msgs)} 回退到对应位置，输入 q 取消"
        
        self.io.info(lines[0])
        for l in lines[1:]:
            self.io.item(l)
        self.io.hint(prompt_hint)
        
        choice = await self.io.ask("AI <- 选择: ")
        
        if not choice or choice.lower() == "q":
            msg = "已取消"
            self.io.info(msg)
            return msg
        
        try:
            idx_in_history = int(choice) - 1
            if idx_in_history < 0 or idx_in_history >= len(history_msgs):
                msg = f"❌ 无效选择，请输入 1~{len(history_msgs)}"
                self.io.error(msg)
                return msg
        except ValueError:
            msg = "❌ 请输入数字"
            self.io.error(msg)
            return msg
        
        sys_count = sum(1 for m in self._context if m["role"] == "system")
        del self._context[sys_count + idx_in_history + 1:]
        self.session.save_context(self._context)
        
        msg = f"⏪ 已回退到第 {choice} 条消息位置"
        self.io.info(msg)
        return msg
    
    def _cmd_fork(self):
        """基于当前上下文新建会话"""
        old_messages = [m for m in self._context if m["role"] != "system"]
        
        if not old_messages:
            display.info("当前会话没有消息，无法 fork")
            return
        
        self.session.save_context(self._context)
        
        summary = ""
        if old_messages:
            last_msg = old_messages[-1]
            summary = last_msg.get("content", "")[:50]
        
        old_sid = self.session.session_id
        new_sid = self.session.create_session()
        
        self._context = [{"role": "system", "content": self._load_system_prompt()}]
        for m in old_messages:
            self._context.append(m)
        self.session.save_context(self._context)
        
        self.session.update_meta(old_sid, summary=summary)
        
        self._context = self._build_context()
        
        display.info(f"🍴 已 fork：从 {old_sid} → {new_sid}")
    
    def _cmd_history(self):
        """查看当前对话历史"""
        history_msgs = [m for m in self._context if m["role"] != "system"]
        
        if not history_msgs:
            display.info("暂无对话历史")
            return
        
        roles_zh = {"user": "👤 用户", "assistant": "🤖 AI", "tool": "🔧 工具"}
        display.info(f"\n📜 对话历史（共 {len(history_msgs)} 条）:")
        print()
        
        for i, msg in enumerate(history_msgs):
            role = roles_zh.get(msg["role"], msg["role"])
            content = msg.get("content", "")
            if msg["role"] == "tool":
                content = content[:80] + "..." if len(content) > 80 else content
            else:
                content = content[:120] + "..." if len(content) > 120 else content
            content = content.replace("\n", " ")
            display.item(f"  [{i+1:3d}] {role}: {content}")
        
        print()
    
    # ============ 命令处理 ============
    
    @property
    def COMMANDS(self):
        """动态获取命令列表（从 commands 模块）"""
        cmds = get_all_commands()
        return {f"/{name}": desc for name, desc in cmds.items()}
    
    async def handle_command(self, cmd_line: str) -> tuple[bool, str]:
        """处理斜杠命令，返回 (已处理, 输出文本)"""
        if not cmd_line.strip().startswith("/"):
            return (False, "")
        
        parts = cmd_line.strip().split(maxsplit=1)
        cmd = parts[0].lstrip("/").lower()
        arg = parts[1] if len(parts) > 1 else ""
        
        return await execute_command(self, cmd, arg)
    
    # ============ 主处理流程（全异步） ============
    
    async def process(self, user_input: str,
                      io: Optional[IOChannel] = None) -> Response:
        """
        处理用户输入（全异步）
        
        io: 可选 IO 通道覆盖。WebUI 模式传入 WebSocketIO，
            此时命令内部的 self.io 暂时被替换。
        """
        await self._ensure_initialized()
        
        if not user_input.strip():
            return Response(content="")
        
        # 临时替换 IO 通道
        old_io = self.io
        if io is not None:
            self.io = io
        
        try:
            return await self._process_inner(user_input)
        finally:
            self.io = old_io
    
    async def _process_inner(self, user_input: str) -> Response:
        """处理用户输入的核心逻辑（io 已在 process() 中设置好）"""
        
        # 检查命令
        if user_input.strip().startswith("/"):
            handled, output = await self.handle_command(user_input)
            if handled:
                return Response(content=output)
        
        # 添加用户消息
        self._context.append({"role": "user", "content": user_input})
        
        # 生命周期：消息已接收
        await self.lifecycle.emit(LifecycleHook.ON_MESSAGE_RECEIVED, content=user_input)
        
        iteration = 0
        last_text = ""
        
        while True:
            iteration += 1
            
            # 循环检测
            loop, reason = self._loop_detector.check(iteration, last_text)
            if loop:
                display.warning(f"\n⚠️  {reason}，强制跳出循环\n")
                self._context.append({
                    "role": "system",
                    "content": f"系统提示：{reason}。请停止操作并总结。"
                })
                break
            
            # 发送前确保 tool 消息完整性
            self._context = [self._context[0]] + self._repair_tool_ordering(self._context[1:])
            
            # 生命周期：LLM 调用前
            await self.lifecycle.emit(LifecycleHook.ON_BEFORE_LLM_CALL)
            
            self._processing = True
            try:
                assistant_msg = await self._stream_chat(self._context)
            except (asyncio.CancelledError, KeyboardInterrupt):
                # CancelledError 在 _stream_chat 内部已被捕获（含 partial content），
                # 但若仍逃逸至此（理论不应发生），直接放行让上层处理
                self._processing = False
                raise
            except Exception as e:
                self._processing = False
                display.error(f"API/LLM 错误: {e}")
                await self.lifecycle.emit(LifecycleHook.ON_ERROR, error=str(e))
                err_str = str(e)
                if "'tool'" in err_str and "preceding" in err_str:
                    display.warning("  🔧 检测到 tool 顺序错误，二次修复...")
                    repaired = self._repair_tool_ordering(self._context[1:])
                    self._context = [self._context[0]] + repaired
                    try:
                        self._processing = True
                        assistant_msg = await self._stream_chat(self._context)
                    except (asyncio.CancelledError, KeyboardInterrupt):
                        self._processing = False
                        raise
                    except Exception as e2:
                        self._processing = False
                        display.error(f"  ❌ 修复后仍失败: {e2}")
                        break
                else:
                    break
            self._processing = False
            
            # 生命周期：LLM 调用完成
            tc_names = [tc["function"]["name"] for tc in assistant_msg.get("tool_calls", [])] if assistant_msg.get("tool_calls") else []
            await self.lifecycle.emit(LifecycleHook.ON_AFTER_LLM_CALL, has_tool_calls=bool(tc_names), tool_names=tc_names, content=assistant_msg.get("content", ""))
            
            # === 流式中断处理：加法填充上下文 ===
            interrupted = assistant_msg.pop("_interrupted", False)
            if interrupted and "tool_calls" in assistant_msg:
                # tool_calls 已生成但未执行 → 退化为文本回复
                tc_names = [tc["function"]["name"] for tc in assistant_msg["tool_calls"]]
                content = assistant_msg.get("content", "")
                note = f"\n\n[用户中断 — 计划调用的工具: {', '.join(tc_names)}，请求已被用户打断]"
                assistant_msg["content"] = (content + note) if content else note.strip()
                del assistant_msg["tool_calls"]
            
            # 始终 append assistant 消息（包括中断时的部分内容）
            self._context.append(assistant_msg)
            
            if interrupted:
                display.info("⏹️ 已中断（保留了已生成的内容）")
                break
            
            text_content = assistant_msg.get("content", "")
            if not text_content:
                tool_names = [tc["function"]["name"] for tc in assistant_msg.get("tool_calls", [])]
                text_content = f"[调用了工具: {', '.join(tool_names)}]"
            else:
                last_text = text_content
            
            # 处理工具调用
            tool_calls = assistant_msg.get("tool_calls", [])
            if tool_calls:
                # 生命周期：工具选择
                sel_tool_names = [tc["function"]["name"] for tc in tool_calls]
                await self.lifecycle.emit(LifecycleHook.ON_TOOL_SELECT, tools=sel_tool_names)
                
                tool_interrupted = False
                for i, tc in enumerate(tool_calls):
                    try:
                        # 生命周期：工具调用（执行前）
                        await self.lifecycle.emit(LifecycleHook.ON_TOOL_CALL, tool_name=tc["function"]["name"], tool_args=tc["function"]["arguments"][:200])
                        result = await self._execute_tool(tc)
                        self._context.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result
                        })
                        # 生命周期：工具调用完成
                        await self.lifecycle.emit(LifecycleHook.ON_TOOL_RESULT, tool_name=tc["function"]["name"], result=result[:200])
                    except (KeyboardInterrupt, asyncio.CancelledError):
                        self._interrupted = False  # 重置，防止残留
                        # 当前工具标记为调用失败
                        self._context.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": "工具调用失败：用户中断"
                        })
                        # 剩余工具标记为未执行
                        for remaining in tool_calls[i + 1:]:
                            self._context.append({
                                "role": "tool",
                                "tool_call_id": remaining["id"],
                                "content": "未执行（工具调用失败：用户中断）"
                            })
                        display.info("⏹️ 已中断（上下文已保留工具调用信息）")
                        tool_interrupted = True
                        break  # 退出 for 循环
                
                if tool_interrupted:
                    break  # 退出 while 循环
                
                continue
            else:
                break
        
        # 裁剪上下文
        self._context = self._trim_context(self._context)
        
        # 生命周期：上下文已更新
        await self.lifecycle.emit(LifecycleHook.ON_CONTEXT_UPDATE, msg_count=len(self._context))
        
        # 保存上下文
        self.session.save_context(self._context)
        
        # 提取最终回复
        final_content = ""
        for msg in reversed(self._context):
            if msg["role"] == "assistant" and msg.get("content"):
                final_content = msg["content"]
                break
        
        # 清理中断标志（防止状态残留）
        self._interrupted = False
        self._processing = False
        
        # 生命周期：返回响应前
        await self.lifecycle.emit(LifecycleHook.ON_BEFORE_RESPONSE, content=final_content)
        
        return Response(content=final_content)
    
    async def _ensure_initialized(self):
        """确保已初始化"""
        if not hasattr(self, "_initialized"):
            self._initialized = True
            await self.lifecycle.emit(LifecycleHook.ON_INIT)
    
    async def shutdown(self):
        """关闭 Agent（异步）"""
        # 核弹退出：跳过所有保存逻辑
        if getattr(self, "_nuclear_exit", False):
            await self.lifecycle.emit(LifecycleHook.ON_SHUTDOWN)
            await self.lifecycle.emit(LifecycleHook.ON_CLEANUP)
            # 关闭 LLM 客户端
            await self.client.close()
            return
        
        # 保存上下文
        self.session.save_context(self._context)
        
        # 用 LLM 生成会话总结
        summary = ""
        try:
            history_msgs = [m for m in self._context if m["role"] != "system"]
            if len(history_msgs) >= 2:
                summary_context = self._context + [
                    {"role": "user", "content": "请总结一下，给这次对话起一个5到10个汉字的名字。不要添加任何多余的文字。"}
                ]
                assistant_msg = await self._stream_chat(summary_context, silent=True)
                summary = assistant_msg.get("content", "").strip()
        except Exception:
            pass
        
        # 回退：取首条用户消息
        if not summary:
            for m in self._context:
                if m["role"] == "user":
                    text = m.get("content", "").strip()
                    if text:
                        summary = text.split("\n")[0].strip()[:50]
                        break
        if not summary:
            summary = "empty_session"
        
        # 更新内嵌 meta（summary）
        self.session.update_meta(
            self.session.session_id,
            summary=summary,
        )
        
        # 显示退出面板
        info = self.session.list_sessions().get(self.session.session_id, {})
        msg_count = info.get("message_count", 0)
        created = info.get("created", "?")
        duration = ""
        try:
            delta = datetime.now() - datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
            h, r = divmod(int(delta.total_seconds()), 3600)
            m, s = divmod(r, 60)
            duration = f"{h}:{m:02d}:{s:02d}"
        except Exception:
            pass
        
        display.shutdown_panel(
            summary=summary,
            file=f"{self.session.session_id}.jsonl",
            model=self.model,
            msg_count=msg_count,
            created=created,
            duration=duration,
        )
        
        await self.lifecycle.emit(LifecycleHook.ON_SHUTDOWN)
        await self.lifecycle.emit(LifecycleHook.ON_CLEANUP)
        
        # 关闭 LLM 客户端
        await self.client.close()
