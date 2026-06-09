"""
Agent 主干类（全异步版本 — 重构版）

职责收敛为"编排层"：
- 持有注入的服务（ConversationState, LLMService, ToolExecutor, PromptBuilder, SessionManager）
- 主循环 _process_inner 只做流程控制，具体操作委托给服务
- 生命周期 emit() 返回值被实际消费，插件能 transform/guard 流程
- 不直接持有 _context — ConversationState 是唯一的事实源
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
from core.conversation import ConversationState, CompactConfig
from core.llm_service import LLMService, LLMConfig
from core.tool_executor import ToolExecutor
from core.prompt_builder import PromptBuilder

# ── 跨平台中断标志 ─────────────────────────────────────
_interrupted_flag = False
from plugins.base.plugin import Plugin, PluginRegistry, PluginConfig

import config
import tools
import display
from core import session
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
    完整 Agent 实现（全异步 — 服务化重构）
    
    职责：编排主循环 + 触发生命周期
    委托：ConversationState / LLMService / ToolExecutor / PromptBuilder / SessionManager
    
    特性：
    - 生命周期驱动的插件系统（emit 返回值被消费）
    - 会话管理（持久化通过 SessionStore）
    - 技能系统（热重载通过 PromptBuilder）
    - 工具调用（循环执行通过 ToolExecutor）
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
        
        # ── 创建 LLM client ──
        from core.llm_client import Client
        self.client = Client(
            api_key=config.LLM_API_KEY,
            base_url=config.LLM_API_BASE_URL
        )
        
        # ── 注入服务 ─────────────────────────────────
        
        # PromptBuilder：系统提示词构建
        from skills.loader import skill_loader
        self._prompter = PromptBuilder(skill_loader=skill_loader)
        
        # LLMService：纯 LLM 调用
        llm_config = LLMConfig(
            model=config.LLM_MODEL,
            temperature=config.LLM_TEMPERATURE,
            max_tokens=config.LLM_MAX_TOKENS,
        )
        self._llm = LLMService(self.client, llm_config)
        
        # ToolExecutor：工具执行
        self._tool_exec = ToolExecutor()
        
        # ConversationState：上下文状态的唯一所有者
        initial_prompt = self._prompter.build_system_prompt()
        self._conv = ConversationState(initial_prompt)
        
        # SessionManager：持久化（不再持有 _context）
        self.session = session.SessionManager(resume=resume)
        os.makedirs(config.SESSIONS_DIR, exist_ok=True)
        
        if not os.environ.get("FP_SUBAGENT_QUIET"):
            display.info(f"📂 新会话：{self.session.session_id}")
        
        # 从会话文件恢复历史
        saved = self.session.load_context(initial_prompt)
        if len(saved) > 1:
            self._conv.replace_all(saved)
        
        # 生命周期管理器
        self.lifecycle = LifecycleManager(enable_log=enable_log)
        
        # 插件注册表
        _plugin_dir = os.path.join(os.path.dirname(__file__), '..', 'plugins')
        self.plugins = PluginRegistry(
            self.lifecycle,
            plugin_dir=os.path.normpath(_plugin_dir),
        )
        
        # 核弹退出标记
        self._nuclear_exit = False
        
        # 中断标记
        self._interrupted = False
        self._processing = False
        self._cancelled_by_user = False
        
        # 循环检测器
        self._loop_detector = session.LoopDetector(
            max_iterations=config.MAX_ITERATIONS
        )
        
        # 内置钩子
        self._register_builtin_hooks()
        
        # 触发初始化生命周期
        # （init 在 _ensure_initialized 中触发）
    
    # ── 兼容旧代码：_context 桥接属性 ──
    # 过渡期：命令和 WebUI 尚未完全迁移时，_context 仍可读写
    # 长期目标：所有代码通过 ConversationState 公共 API 操作
    
    @property
    def _context(self) -> List[Dict]:
        """兼容旧代码 — 返回内部列表引用（过渡期桥接）"""
        return self._conv._messages
    
    @_context.setter
    def _context(self, value: List[Dict]):
        self._conv.replace_all(value)
    
    @property
    def _system_prompt(self) -> str:
        return self._conv.system_prompt
    
    def _build_context(self) -> List[Dict]:
        """兼容旧代码 — 构建新版上下文并返回"""
        prompt = self._prompter.build_system_prompt()
        self._conv.set_system_prompt(prompt)
        saved = self.session.load_context(prompt)
        if len(saved) > 1:
            self._conv.replace_all(saved)
        return self._conv._messages
    
    # ── 内置钩子 ─────────────────────────────────────
    
    def _register_builtin_hooks(self):
        self.lifecycle.register(
            LifecycleHook.ON_INIT, self._on_init, priority=0, name="builtin_init"
        )
        self.lifecycle.register(
            LifecycleHook.ON_SHUTDOWN, self._on_shutdown, priority=999, name="builtin_shutdown"
        )
    
    async def _on_init(self, ctx: HookContext, **kwargs) -> HookContext:
        if self.enable_log:
            print("[Agent] Initializing...")
        ctx.data["initialized"] = True
        return ctx
    
    async def _on_shutdown(self, ctx: HookContext, **kwargs) -> HookContext:
        """关闭钩子 — 保存上下文"""
        self.session.save_context(self._conv.messages)
        if self.enable_log:
            print("[Agent] Shutting down...")
        return ctx
    
    # ── 技能系统 ─────────────────────────────────────
    
    def reload_skills(self) -> bool:
        """热重载技能"""
        try:
            display.info("🔄 正在重新加载技能...")
            count = self._prompter.reload_skills()
            self._conv.set_system_prompt(self._prompter.build_system_prompt())
            display.info(f"✅ 技能重载完成！当前可用技能数：{count}")
            
            from skills.loader import skill_loader
            for name, skill in sorted(skill_loader.skills.items()):
                title = skill.title if hasattr(skill, 'title') else name
                display.item(f"   • {title} ({name})")
            
            return True
        except Exception as e:
            display.error(f"❌ 技能重载失败：{e}")
            return False
    
    # ── 命令辅助：添加/删除技能 ─────────────────────
    
    def _cmd_add_skill(self, filename: str):
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
        
        from skills.loader import skill_loader as sl
        skill_file = os.path.join(config.SKILLS_DIR, filename)
        
        if not os.path.exists(skill_file):
            display.error(f"❌ 错误：文件不存在 {skill_file}")
            return
        
        try:
            skill = sl._load_skill_file(skill_file)
            if skill is None:
                display.error(f"❌ 错误：无法加载技能文件 {skill_file}")
                return
            
            if skill.name in sl.skills:
                display.warning(f"⚠️ 警告：技能 '{skill.name}' 已存在！")
                display.hint(f"   如需更新，请先删除旧版本或使用 /reload_skills")
                return
            
            sl.skills[skill.name] = skill
            self._conv.set_system_prompt(self._prompter.build_system_prompt())
            
            # 重建上下文
            saved = self.session.load_context(self._conv.system_prompt)
            if len(saved) > 1:
                self._conv.replace_all(saved)
            
            display.success(f"✅ 技能 '{skill.name}' 已添加")
            display.item(f"   标题: {skill.title}")
            display.item(f"   描述: {skill.description}")
        except Exception as e:
            display.error(f"❌ 添加技能失败：{e}")
    
    def _cmd_remove_skill(self, skill_name: str):
        from skills.loader import skill_loader as sl
        if not skill_name:
            display.info("❌ 用法：/remove_skill <skill_name>")
            display.item("   示例：/remove_skill web_search")
            return
        
        skill_name = skill_name.lower().replace(" ", "_")
        if skill_name not in sl.skills:
            all_names = ", ".join(sorted(sl.skills.keys()))
            display.error(f"❌ 未找到技能 '{skill_name}'")
            display.hint(f"   可用技能: {all_names}")
            return
        
        skill = sl.skills.pop(skill_name)
        self._conv.set_system_prompt(self._prompter.build_system_prompt())
        
        # 重建上下文
        saved = self.session.load_context(self._conv.system_prompt)
        if len(saved) > 1:
            self._conv.replace_all(saved)
        
        display.success(f"✅ 技能 '{skill.title}' 已移除")
    
    def list_skills(self) -> str:
        """列出所有技能"""
        from skills.loader import skill_loader
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
            saved = self.session.load_context(self._conv.system_prompt)
            self._conv.replace_all(saved)
            return True
        return False
    
    def clear_session(self):
        """清空当前会话"""
        system_prompt = self._prompter.build_system_prompt()
        self._conv.reset(system_prompt)
        # 重置 meta 并清空文件
        from core.session import _default_meta
        meta = _default_meta(self.session.session_id)
        path = self.session._session_path()
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
    
    def delete_session(self, sid: str) -> bool:
        return self.session.delete_session(sid)
    
    # ============ 中断机制 ============
    
    def cancel(self):
        self._interrupted = True
    
    def _check_interrupted(self):
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
    
    # ============ LLM 调用（含 IO 展示） ============
    
    async def _stream_chat(self, context: List[Dict], silent: bool = False) -> Dict:
        """发起聊天请求（含 spinner 和流式展示）
        
        实际 LLM 调用委托给 LLMService.chat()，
        此方法只负责 spinner/streamer 等 IO 展示。
        """
        spinner = None
        if not silent:
            spinner = display.Spinner("思考中")
            await spinner.start()
        
        try:
            assistant_msg = await self._llm.chat(context, tools=self._tool_exec.get_definitions())
        except asyncio.CancelledError:
            self._cancelled_by_user = True
            msg = {"role": "assistant", "content": "", "_interrupted": True}
            return msg
        finally:
            if spinner:
                await spinner.stop()
        
        reply_content = assistant_msg.get("content", "")
        
        streamer = display.LLMStreamer(silent=silent)
        if reply_content:
            streamer.write(reply_content)
        streamer.end()
        
        msg = {"role": "assistant", "content": reply_content}
        if assistant_msg.get("tool_calls"):
            msg["tool_calls"] = assistant_msg["tool_calls"]
        msg["_interrupted"] = False
        
        return msg
    
    async def _compact_context(self):
        """压缩上下文 — 委托给 ConversationState.compact() + LLM 摘要"""
        history_count = self._conv.get_non_system_count()
        if history_count <= 4:
            display.info("对话历史较短，无需压缩")
            return
        
        display.info("🔄 正在压缩对话历史...")
        
        async def summarizer(text: str) -> str:
            try:
                return await self._llm.summarize(text)
            except Exception as e:
                display.error(f" 压缩失败: {e}")
                return ""
        
        did_compact, msg = await self._conv.compact(
            summarizer=summarizer,
            config=CompactConfig(keep_meaningful=4)
        )
        
        if did_compact:
            display.info(f" ✅\n📦 {msg}")
        else:
            display.info(msg)
    
    # ============ 工具展示 ============
    
    async def _execute_tool(self, tc: Dict, silent: bool = False) -> str:
        """执行工具（含展示）"""
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"])
        except json.JSONDecodeError as e:
            return f"错误：工具参数 JSON 解析失败 - {e}"
        
        if not silent:
            safe_args = {k: str(v) for k, v in args.items()}
            display.llm_tool(f"  🛠️  {name}({json.dumps(safe_args, ensure_ascii=False)})")
        
        try:
            result = await self._tool_exec.execute(tc)
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as e:
            return f"❌ 工具执行失败 ({name}): {e}"
        
        if not silent:
            display.llm_tool(f"  📋  {result.strip()}")
        
        return result
    
    # ============ 命令处理 ============
    
    @property
    def COMMANDS(self):
        cmds = get_all_commands()
        return {f"/{name}": desc for name, desc in cmds.items()}
    
    async def handle_command(self, cmd_line: str) -> tuple[bool, str]:
        """处理斜杠命令"""
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
        
        io: 可选 IO 通道覆盖。WebUI 模式传入 WebSocketIO
        """
        await self._ensure_initialized()
        
        if not user_input.strip():
            return Response(content="")
        
        old_io = self.io
        if io is not None:
            self.io = io
        
        try:
            return await self._process_inner(user_input)
        finally:
            self.io = old_io
    
    async def _process_inner(self, user_input: str) -> Response:
        """处理用户输入的核心逻辑"""
        
        self._cancelled_by_user = False
        
        # ── 检查命令 ──
        if user_input.strip().startswith("/"):
            handled, output = await self.handle_command(user_input)
            if handled:
                ctx = await self.lifecycle.emit(
                    LifecycleHook.ON_BEFORE_RESPONSE, content=output
                )
                return Response(content=output)
        
        # ── 生命周期：MESSAGE_FILTER（插件可 transform/拒绝） ──
        ctx = await self.lifecycle.emit(
            LifecycleHook.ON_MESSAGE_FILTER,
            content=user_input,
            messages=self._conv.messages,
        )
        if ctx.data.get("blocked"):
            return Response(content=ctx.data.get("block_reason", "消息被插件过滤"))
        filtered_input = ctx.data.get("filtered_content", user_input)
        
        # ── 添加用户消息 ──
        self._conv.add_user_message(filtered_input)
        
        # ── 生命周期：消息已接收 ──
        await self.lifecycle.emit(LifecycleHook.ON_MESSAGE_RECEIVED, content=filtered_input)
        
        iteration = 0
        last_text = ""
        
        while True:
            iteration += 1
            
            # 循环检测
            loop, reason = self._loop_detector.check(iteration, last_text)
            if loop:
                display.warning(f"\n⚠️  {reason}，强制跳出循环\n")
                self._conv.add_system_message(f"系统提示：{reason}。请停止操作并总结。")
                break
            
            # 修复 tool ordering
            self._conv.repair_tool_ordering()
            
            # ── 生命周期：BEFORE_LLM_CALL（插件可修改 messages / 取消） ──
            ctx = await self.lifecycle.emit(
                LifecycleHook.ON_BEFORE_LLM_CALL,
                messages=self._conv.messages,
                tools=self._tool_exec.get_definitions(),
            )
            if ctx.data.get("cancelled"):
                return Response(content=ctx.data.get("cancel_reason", "已取消"))
            
            messages_for_llm = ctx.data.get("modified_messages", self._conv.messages)
            
            self._processing = True
            try:
                assistant_msg = await self._stream_chat(messages_for_llm)
            except (asyncio.CancelledError, KeyboardInterrupt):
                self._processing = False
                raise
            except Exception as e:
                self._processing = False
                display.error(f"API/LLM 错误: {e}")
                await self.lifecycle.emit(LifecycleHook.ON_ERROR, error=str(e))
                err_str = str(e)
                if "'tool'" in err_str and "preceding" in err_str:
                    display.warning("  🔧 检测到 tool 顺序错误，二次修复...")
                    self._conv.repair_tool_ordering()
                    try:
                        self._processing = True
                        assistant_msg = await self._stream_chat(self._conv.messages)
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
            
            # ── 生命周期：AFTER_LLM_CALL（插件可修改回复 / 拦截工具执行） ──
            tc_names = [tc["function"]["name"] for tc in assistant_msg.get("tool_calls", [])]
            ctx = await self.lifecycle.emit(
                LifecycleHook.ON_AFTER_LLM_CALL,
                response=assistant_msg,
                has_tool_calls=bool(tc_names),
                tool_names=tc_names,
                content=assistant_msg.get("content", ""),
            )
            if ctx.data.get("modified_response"):
                assistant_msg = ctx.data["modified_response"]
            if ctx.data.get("block_tool_execution"):
                # 插件阻止了工具执行
                assistant_msg.pop("tool_calls", None)
            
            # 流式中断处理
            interrupted = assistant_msg.pop("_interrupted", False)
            if interrupted and "tool_calls" in assistant_msg:
                tc_names = [tc["function"]["name"] for tc in assistant_msg["tool_calls"]]
                content = assistant_msg.get("content", "")
                note = f"\n\n[用户中断 — 计划调用的工具: {', '.join(tc_names)}，请求已被用户打断]"
                assistant_msg["content"] = (content + note) if content else note.strip()
                del assistant_msg["tool_calls"]
            
            self._conv.add_assistant_message(assistant_msg)
            
            if interrupted:
                display.info("⏹️ 已中断（保留了已生成的内容）")
                break
            
            text_content = assistant_msg.get("content", "")
            if not text_content:
                tool_names = [tc["function"]["name"] for tc in assistant_msg.get("tool_calls", [])]
                text_content = f"[调用了工具: {', '.join(tool_names)}]"
            else:
                last_text = text_content
            
            # ── 处理工具调用 ──
            tool_calls = assistant_msg.get("tool_calls", [])
            tool_interrupted = False
            if tool_calls:
                sel_tool_names = [tc["function"]["name"] for tc in tool_calls]
                await self.lifecycle.emit(LifecycleHook.ON_TOOL_SELECT, tools=sel_tool_names)
                
                for i, tc in enumerate(tool_calls):
                    try:
                        await self.lifecycle.emit(
                            LifecycleHook.ON_TOOL_CALL,
                            tool_name=tc["function"]["name"],
                            tool_args=tc["function"]["arguments"][:200],
                        )
                        result = await self._execute_tool(tc)
                        self._conv.add_tool_message(tc["id"], result)
                        await self.lifecycle.emit(
                            LifecycleHook.ON_TOOL_RESULT,
                            tool_name=tc["function"]["name"],
                            result=result[:200],
                        )
                    except (KeyboardInterrupt, asyncio.CancelledError):
                        self._interrupted = False
                        self._cancelled_by_user = True
                        self._conv.add_tool_message(tc["id"], "工具调用失败：用户中断")
                        for remaining in tool_calls[i + 1:]:
                            self._conv.add_tool_message(
                                remaining["id"],
                                "未执行（工具调用失败：用户中断）"
                            )
                        display.info("⏹️ 已中断（上下文已保留工具调用信息）")
                        tool_interrupted = True
                        break
                
                if tool_interrupted:
                    break
                continue
            else:
                break
        
        # ── 生命周期：上下文已更新 ──
        await self.lifecycle.emit(
            LifecycleHook.ON_CONTEXT_UPDATE,
            msg_count=len(self._conv),
        )
        
        # 保存上下文
        self.session.save_context(self._conv.messages)
        
        # 提取最终回复
        final_content = self._conv.get_last_content()
        
        # 清理中断标志
        self._interrupted = False
        self._processing = False
        
        # ── 生命周期：返回响应前（插件可修改最终回复） ──
        ctx = await self.lifecycle.emit(
            LifecycleHook.ON_BEFORE_RESPONSE,
            content=final_content,
            session_id=self.session.session_id,
        )
        final_content = ctx.data.get("modified_content", final_content)
        
        return Response(content=final_content)
    
    # ============ 生命周期管理 ============
    
    async def _ensure_initialized(self):
        """确保已触发初始化钩子"""
        if not hasattr(self, '_initialized'):
            self._initialized = True
            await self.lifecycle.emit(LifecycleHook.ON_INIT)
    
    async def shutdown(self):
        """关闭 Agent"""
        await self.lifecycle.emit(LifecycleHook.ON_SHUTDOWN)
        await self.lifecycle.emit(LifecycleHook.ON_CLEANUP)
        
        # _on_shutdown 钩子已保存上下文，此处只需处理核弹模式
        if self._nuclear_exit:
            self.session.delete_session(self.session.session_id)
            display.info("💥 核弹模式：当前会话已删除，不留痕迹")
        
        display.info(f"👋 Agent 已关闭")
