import argparse
import json
import os
import re
import sys
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import openai

import config
import tools
from memory import Memory
from skills_loader import skill_loader


class Agent:
    def __init__(self, resume_mode: bool = False):
        self.client = openai.Client(api_key=config.OPENAI_API_KEY, base_url=config.OPENAI_API_BASE_URL)
        self.model = config.MODEL_NAME
        self.memory = Memory()
        tools.init_tasks(config.TASKS_FILE)
        self._input_session: Any = None  # PromptSession or None/False
        
        # 技能系统初始化
        skill_loader.load_all()
        self.skills = skill_loader.skills.copy()
        self._update_system_prompt()  # 加载初始系统提示词

        # 会话管理
        os.makedirs(config.SESSIONS_DIR, exist_ok=True)
        if resume_mode:
            # 恢复模式：接着最近结束的 session 继续开启对话
            self._session_id = self._init_resume_session()
            print(f"🔄 已接着最近会话继续：{self._session_id}")
        else:
            self._session_id = self._init_session()
            print(f"📂 新会话：{self._session_id}")
            print(f"💡 输入 /help 查看命令，/resume 可回到历史会话")
    # ── 会话管理 ──────────────────────────────

    def _init_resume_session(self) -> str:
        """接着最近结束的 session 继续开启对话。"""
        meta = self._load_meta()
        sessions = meta.get("sessions", {})

        if not sessions:
            print("⚠️ 没有找到历史会话，将创建新会话")
            return self._init_session()

        # 按创建时间降序排列，找到第一个有消息的会话
        sorted_sids = sorted(sessions.keys(), reverse=True)
        recent_sid = None
        for sid in sorted_sids:
            info = sessions[sid]
            if info.get("message_count", 0) > 0:
                recent_sid = sid
                break

        if recent_sid is None:
            print("⚠️ 没有找到有效会话，将创建新会话")
            return self._init_session()

        # 读取最近会话的历史记录
        recent_info = sessions[recent_sid]
        recent_filename = recent_info.get("file", f"{recent_sid}.jsonl")
        recent_path = os.path.join(config.SESSIONS_DIR, recent_filename)

        # 创建新会话
        new_sid = self._init_session()
        new_info = meta["sessions"][new_sid]
        new_filename = new_info.get("file", f"{new_sid}.jsonl")
        new_path = os.path.join(config.SESSIONS_DIR, new_filename)

        # 复制历史记录到新会话
        if os.path.exists(recent_path):
            import shutil
            shutil.copy2(recent_path, new_path)

        # 更新元数据
        meta["current"] = new_sid
        self._save_meta(meta)

        return new_sid

    def _session_history_path(self) -> str:
        """返回当前会话的历史文件路径（优先使用 meta 中记录的实际文件名）。"""
        meta = self._load_meta()
        info = meta.get("sessions", {}).get(self._session_id, {})
        filename = info.get("file", f"{self._session_id}.jsonl")
        return os.path.join(config.SESSIONS_DIR, filename)

    @staticmethod
    def _meta_path() -> str:
        return os.path.join(config.SESSIONS_DIR, "_meta.json")

    def _load_meta(self) -> dict:
        path = self._meta_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"sessions": {}, "current": None}

    def _save_meta(self, meta: dict):
        with open(self._meta_path(), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    def _init_session(self) -> str:
        """创建新会话，返回 session ID。"""
        sid = datetime.now().strftime("s_%y%m%d_%H%M%S")
        meta = self._load_meta()
        meta["sessions"][sid] = {
            "id": sid,
            "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "updated": "",
            "summary": "",
            "message_count": 0,
        }
        meta["current"] = sid
        self._save_meta(meta)
        return sid

    def _switch_session(self, sid: str, context: list[dict]) -> bool:
        """切换到指定会话。支持 session ID 或完整文件名。返回是否成功。"""
        meta = self._load_meta()
        sessions = meta.get("sessions", {})

        # 如果输入是完整文件名，提取 session ID
        if sid not in sessions:
            m = re.match(r"(s_\d{6}_\d{6})", sid)
            if m and m.group(1) in sessions:
                sid = m.group(1)

        if sid not in sessions:
            print(f"未找到会话: {sid}")
            print(f"使用 /resume list 查看可用会话")
            return False
        self._session_id = sid
        meta["current"] = sid
        self._save_meta(meta)
        context.clear()
        context.extend(self._build_context())
        info = meta["sessions"][sid]
        print(f"📂 已切换到会话: {sid}  ({info.get('message_count', 0)}条消息)")
        return True

    # ── 输入处理 ──────────────────────────────

    def _ensure_input_session(self):
        """懒初始化 prompt_toolkit 会话（只在交互模式首次输入时调用）。"""
        if self._input_session is not None:
            return
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.history import FileHistory
            from prompt_toolkit.key_binding import KeyBindings

            kb = KeyBindings()
            @kb.add("c-c")
            def _exit_(event):
                event.app.exit(exception=KeyboardInterrupt)

            self._input_session = PromptSession(
                history=FileHistory(os.path.join(config.MEMORY_DIR, "_input_history")),
                key_bindings=kb,
                message="AI <- ",
            )
        except ImportError:
            self._input_session = False  # False = 不可用, None = 未初始化

    def _get_user_input(self) -> str:
        """获取用户输入，优先使用 prompt_toolkit。"""
        self._ensure_input_session()
        if self._input_session:
            return self._input_session.prompt()
        try:
            return input("AI <- ")
        except EOFError:
            raise

    # ── 斜杠命令 ──────────────────────────────

    COMMANDS = {
        "/help": "显示此帮助",
        "/clear": "清空当前会话历史",
        "/resume": "管理/切换历史会话",
        "/session": "显示当前会话信息",
        "/tasks": "显示待办任务列表",
        "/memory": "管理持久化记忆",
        "/model": "显示当前模型配置",
        "/fork": "基于当前上下文新建会话，关闭旧会话但不退出",
        "/back": "回退到对话的某个历史时刻",
        "/reset": "重置上下文（保留系统提示和记忆）",
        "/skills": "列出所有可用技能",
        "/reload_skills": "热重载所有技能",
        "/add_skill <filename>": "从文件添加新技能",
        "/remove_skill <name>": "删除指定技能",
        "/exit": "退出程序",
    }

    def _show_help(self):
        print("可用命令:")
        for cmd, desc in sorted(self.COMMANDS.items()):
            print(f"  {cmd:12s}  {desc}")

    def _handle_command(self, cmd_line: str, context: list[dict]) -> bool:
        """处理斜杠命令。返回 True 表示已处理，调用方跳过 LLM。"""
        if not cmd_line.strip().startswith("/"):
            return False

        parts = cmd_line.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("/help", "/?"):
            self._show_help()
        elif cmd == "/clear":
            context.clear()
            context.append({"role": "system", "content": self._load_system_prompt()})
            hist = self._session_history_path()
            if os.path.exists(hist):
                os.remove(hist)
            print("🧹 当前会话历史已清空")
        elif cmd == "/resume":
            self._cmd_resume(arg, context)
        elif cmd == "/session":
            self._cmd_session()
        elif cmd == "/tasks":
            print(tools.dispatch("task_list"))
        elif cmd == "/memory":
            self._cmd_memory(arg)
        elif cmd == "/model":
            print(f"模型:   {self.model}")
            print(f"温度:   {config.TEMPERATURE}")
            print(f"最大 Token: {config.MAX_TOKENS}")
            print(f"会话目录: {config.SESSIONS_DIR}")
        elif cmd == "/fork":
            self._cmd_fork(context)
        elif cmd == "/back":
            self._cmd_back(context)
        elif cmd == "/reset":
            context.clear()
            context.extend(self._build_context())
            print("🔄 上下文已重置")
        elif cmd == "/skills":
            print(self.list_skills())
        elif cmd == "/reload_skills":
            if self.reload_skills():
                # 重新构建上下文以应用新技能
                context.clear()
                context.extend(self._build_context())
                print("✅ 系统提示词已更新，新技能立即可用！")
        elif cmd == "/add_skill":
            self._cmd_add_skill(arg, context)
        elif cmd == "/remove_skill":
            self._cmd_remove_skill(arg, context)
        elif cmd in ("/exit", "/quit"):
            raise SystemExit()
        else:
            print(f"❓ 未知命令: {cmd} ")
            self._show_help()
        return True

    def _cmd_session(self):
        meta = self._load_meta()
        info = meta.get("sessions", {}).get(self._session_id, {})
        filename = info.get("file", f"{self._session_id}.jsonl")
        print(f"📂 当前会话: {self._session_id}")
        print(f"   文件: {filename}")
        print(f"   创建: {info.get('created', '?')}")
        print(f"   消息: {info.get('message_count', 0)} 条")

    def _cmd_resume(self, arg: str, context: list[dict]):
        meta = self._load_meta()
        sessions = meta.get("sessions", {})

        if not arg or arg == "list":
            if not sessions:
                print("暂无历史会话")
                return
            current = meta.get("current", "")
            
            # 如果没有任何参数，自动切换到最近的非空会话
            if not arg:
                # 按创建时间降序排列，找到第一个有消息的会话
                sorted_sids = sorted(sessions.keys(), reverse=True)
                recent_sid = None
                for sid in sorted_sids:
                    info = sessions[sid]
                    if info.get("message_count", 0) > 0:
                        recent_sid = sid
                        break
                
                if recent_sid is None:
                    # 如果没有非空会话，使用最新的会话
                    recent_sid = sorted_sids[0]
                
                print(f"🔄 继续最近会话：{recent_sid}")
                self._switch_session(recent_sid, context)
                return
            
            print("📋 历史会话:")
            for sid in sorted(sessions, reverse=True):
                info = sessions[sid]
                marker = " ← 当前" if sid == current else ""
                cnt = info.get("message_count", 0)
                created = info.get("created", "?")
                filename = info.get("file", f"{sid}.jsonl")
                summary = info.get("summary", "")
                tag = f" — {summary}" if summary else ""
                print(f"  {filename}{marker}{tag}  ({cnt}条, {created})")
            return

        if arg.startswith("delete "):
            sid = arg[7:].strip()
            if sid not in sessions:
                m = re.match(r"(s_\d{6}_\d{6})", sid)
                if m and m.group(1) in sessions:
                    sid = m.group(1)
            if sid not in sessions:
                print(f"未找到会话: {sid}")
                return
            info = sessions[sid]
            filename = info.get("file", f"{sid}.jsonl")
            del sessions[sid]
            meta["sessions"] = sessions
            if meta.get("current") == sid:
                meta["current"] = None
            self._save_meta(meta)
            hist = os.path.join(config.SESSIONS_DIR, filename)
            if os.path.exists(hist):
                os.remove(hist)
            print(f"🗑️  已删除会话: {filename}")
            return

        # 切换到指定会话
        self._switch_session(arg, context)

    def _cmd_memory(self, arg: str):
        if not arg or arg == "list":
            mems = self.memory.list_memories()
            if not mems:
                print("暂无记忆")
                return
            print("📋 记忆列表:")
            for m in mems:
                print(f"  [{m['type']}] {m['name']} — {m['description']}")
        elif arg.startswith("save "):
            content = arg[5:].strip()
            if not content:
                print("用法: /memory save <记忆内容>")
                return
            name = f"manual_{len(self.memory.list_memories()) + 1}"
            self.memory.save(name, "reference", f"手动保存 ({config.MODEL_NAME})", content)
            print(f"✅ 记忆已保存: {name}")
        elif arg.startswith("search "):
            kw = arg[7:].strip()
            results = self.memory.search(kw)
            if not results:
                print(f"未找到匹配「{kw}」的记忆")
                return
            print(f"🔍 匹配「{kw}」的记忆:")
            for r in results:
                print(f"  [{r['type']}] {r['name']} — {r['description']}")
        elif arg.startswith("delete "):
            name = arg[7:].strip()
            path = os.path.join(config.MEMORY_DIR, f"{name}.md")
            if os.path.exists(path):
                os.remove(path)
                print(f"🗑️  已删除记忆: {name}")
            else:
                print(f"未找到记忆: {name}")
        else:
            print("子命令: list（默认）, save <内容>, search <关键字>, delete <名称>")

    # ── 上下文管理 ──────────────────────────────

    def _load_system_prompt(self) -> str:
        """加载系统提示词（包含基础提示词 + 所有技能提示词）"""
        parts = []
        
        # 确保技能已加载（第一次调用时自动加载）
        if not skill_loader.skills:
            skill_loader.load_all()
        
        # 加载基础提示词
        agent_md_path = os.path.join(config.PROMPTS_DIR, "agent.md")
        if os.path.exists(agent_md_path):
            with open(agent_md_path, "r", encoding="utf-8") as f:
                parts.append(f.read().strip())
        
        # 加载技能提示词（使用 SkillLoader）
        skill_text = skill_loader.get_all_prompt_text()
        if skill_text:
            parts.append(skill_text)
        
        return "\n\n".join(parts)
    
    def _update_system_prompt(self) -> None:
        """更新系统提示词缓存（用于热重载后刷新）"""
        self.system_prompt_cache = self._load_system_prompt()
    
    def reload_skills(self) -> bool:
        """热重载所有技能
        
        Returns:
            bool: 是否成功重载
        """
        try:
            print("🔄 正在重新加载技能...")
            skill_loader.reload()
            
            # 更新系统提示词
            self._update_system_prompt()
            
            # 显示加载结果
            loaded_count = len(skill_loader.skills)
            print(f"✅ 技能重载完成！当前可用技能数：{loaded_count}")
            
            # 列出所有技能
            if loaded_count > 0:
                print("\n📚 当前可用技能:")
                for name, skill in sorted(skill_loader.skills.items()):
                    title = skill.get('title', 'Unknown')
                    version = skill.get('version', '1.0')
                    category = skill.get('category', 'general')
                    print(f"   • {title} ({name}) - v{version} [{category}]")
            
            return True
            
        except Exception as e:
            print(f"❌ 技能重载失败：{e}")
            import traceback
            traceback.print_exc()
            return False
    
    def list_skills(self) -> str:
        """获取所有技能的列表信息
        
        Returns:
            str: 格式化的技能列表文本
        """
        if not skill_loader.skills:
            skill_loader.load_all()
        
        lines = ["📚 当前可用技能:", ""]
        for name, skill in sorted(skill_loader.skills.items()):
            title = skill.get('title', 'Unknown')
            description = skill.get('description', '')[:50] + '...' if len(skill.get('description', '')) > 50 else skill.get('description', '')
            version = skill.get('version', '1.0')
            category = skill.get('category', 'general')
            priority = skill.get('priority', 5)
            
            lines.append(f"  • {title} ({name})")
            lines.append(f"    版本：v{version} | 类别：{category} | 优先级：{priority}")
            lines.append(f"    描述：{description}")
            lines.append("")
        
        return "\n".join(lines)

    def _cmd_add_skill(self, filename: str, context: list[dict]):
        """从文件添加新技能
        
        Args:
            filename: 技能文件名（位于 skills/ 目录下）
            context: 当前对话上下文
        """
        if not filename:
            print("❌ 用法：/add_skill <filename>")
            print("   示例：/add_skill my_new_skill.md")
            print("\n技能文件格式要求:")
            print("  1. 文件必须位于 skills/ 目录")
            print("  2. 使用 YAML frontmatter 格式:")
            print("     ---")
            print("     name: skill_name")
            print("     title: 技能标题")
            print("     description: 技能描述")
            print("     category: 分类 (可选)")
            print("     version: 版本号 (可选)")
            print("     priority: 优先级 1-10 (可选)")
            print("     ---")
            print("     # 技能正文内容")
            return
        
        skills_dir = Path("skills")
        skills_dir.mkdir(exist_ok=True)
        
        skill_file = skills_dir / filename
        
        # 验证文件扩展名
        if not filename.endswith('.md'):
            print(f"❌ 错误：技能文件必须是 .md 格式")
            return
        
        # 检查文件是否存在
        if not skill_file.exists():
            print(f"❌ 错误：文件不存在 {skill_file}")
            print(f"\n💡 提示：你可以先创建文件，然后再次运行 /add_skill")
            return
        
        try:
            # 加载并验证技能
            skill = skill_loader._load_single(skill_file)
            
            # 检查加载是否成功
            if skill is None:
                print(f"❌ 错误：无法加载技能文件 {skill_file}")
                return
            
            # 检查是否已存在同名技能
            if skill['name'] in skill_loader.skills:
                print(f"⚠️ 警告：技能 '{skill['name']}' 已存在！")
                print(f"   如需更新，请先删除旧版本或使用 /reload_skills")
                return
            
            # 将技能添加到内存中
            skill_loader.skills[skill['name']] = skill
            
            # 重新构建系统提示词
            self._update_system_prompt()
            context.clear()
            context.extend(self._build_context())
            
            print(f"✅ 技能添加成功!")
            print(f"   名称：{skill['title']} ({skill['name']})")
            print(f"   版本：v{skill.get('version', '1.0')}")
            print(f"   类别：{skill.get('category', 'general')}")
            print(f"   优先级：{skill.get('priority', 5)}")
            print(f"   文件：{skill_file}")
            print(f"\n🔄 系统提示词已更新，新技能立即可用！")
            
        except Exception as e:
            print(f"❌ 添加技能失败：{e}")
            import traceback
            traceback.print_exc()

    def _cmd_remove_skill(self, skill_name: str, context: list[dict]):
        """删除指定技能
        
        Args:
            skill_name: 技能名称
            context: 当前对话上下文
        """
        if not skill_name:
            print("❌ 用法：/remove_skill <name>")
            print("   示例：/remove_skill file_manager")
            print("\n当前可用技能列表:")
            if skill_loader.skills:
                for name in sorted(skill_loader.skills.keys()):
                    print(f"   • {name}")
            else:
                print("   暂无可用技能")
            return
        
        # 检查技能是否存在
        if skill_name not in skill_loader.skills:
            print(f"❌ 错误：技能 '{skill_name}' 不存在")
            print("\n当前可用技能列表:")
            if skill_loader.skills:
                for name in sorted(skill_loader.skills.keys()):
                    print(f"   • {name}")
            else:
                print("   暂无可用技能")
            return
        
        # 从内存中移除技能
        del skill_loader.skills[skill_name]
        
        # 重新构建系统提示词
        self._update_system_prompt()
        context.clear()
        context.extend(self._build_context())
        
        print(f"✅ 技能已删除：{skill_name}")
        print(f"🔄 系统提示词已更新，技能已移除！")

    # ── 会话 fork ─────────────────────────────

    def _cmd_fork(self, context: list[dict]):
        """基于当前上下文新建会话，关闭旧会话但不退出。"""
        # 1. 备份当前 non-system 消息
        old_messages = [m for m in context if m["role"] != "system"]
        
        # 2. 关闭旧会话（保存、总结、改名，不污染 context）
        old_sid = self._session_id
        summary = self._close_session(context)
        
        # 3. 创建新会话
        new_sid = self._init_session()
        self._session_id = new_sid
        
        # 4. 将旧消息写入新会话文件
        new_path = self._session_history_path()
        with open(new_path, "w", encoding="utf-8") as f:
            for m in old_messages:
                save_msg = {"role": m["role"], "content": m.get("content", "")}
                if m.get("tool_calls"):
                    save_msg["tool_calls"] = m["tool_calls"]
                if m.get("tool_call_id"):
                    save_msg["tool_call_id"] = m["tool_call_id"]
                f.write(json.dumps(save_msg, ensure_ascii=False) + "\n")
        
        # 5. 重建 context
        context.clear()
        context.extend(self._build_context())
        
        print(f"🍴 已 fork：『{summary}』\n"
              f"   旧会话: {old_sid}\n"
              f"   新会话: {new_sid}（携带 {len(old_messages)} 条历史）")

    # ── 回退功能 ─────────────────────────────

    def _cmd_back(self, context: list[dict]):
        """回退到对话的某个历史时刻。"""
        history_file = self._session_history_path()
        if not os.path.exists(history_file):
            print("没有历史记录可以回退")
            return

        # 从当前 context 提取历史消息（不含 system），按时间正序 [老, ..., 新]
        history_msgs = [m for m in context if m["role"] != "system"]

        if not history_msgs:
            print("没有历史记录可以回退")
            return

        # 显示带编号的消息列表（序号小的为老消息）
        roles_zh = {"user": "👤 用户", "assistant": "🤖 AI", "tool": "🔧 工具"}
        print(f"\n📜 对话历史（共 {len(history_msgs)} 条消息，从上往下为时间顺序）:")
        print()
        
        for i, msg in enumerate(history_msgs):
            role = roles_zh.get(msg["role"], msg["role"])
            content = msg.get("content", "")
            if msg["role"] == "tool":
                content = content[:60] + "..." if len(content) > 60 else content
            else:
                content = content[:120] + "..." if len(content) > 120 else content
            content = content.replace("\n", " ")
            print(f"  [{i+1:3d}] {role}: {content}")

        print()
        print(f"  输入 1~{len(history_msgs)} 回退到对应位置（保留该条及之前的消息）")
        print(f"  输入 q 取消")
        print()

        try:
            choice = input("AI <- 选择: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if choice.lower() == "q":
            print("已取消")
            return

        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(history_msgs):
                print(f"❌ 无效选择，请输入 1~{len(history_msgs)}")
                return
        except ValueError:
            print("❌ 请输入数字")
            return

        # 保留选中的消息及之前的内容，删除之后的新消息
        keep_in_context = idx + 1  # 保留 context 中 system + 前 keep_in_context 条
        del context[keep_in_context + 1:]  # +1 跳过 system
        
        # 写回文件
        self._save_context(context)
        
        print(f"⏪ 已回退到第 {choice} 条消息位置（删除了 {len(history_msgs) - keep_in_context} 条后续消息）")

    def _build_context(self) -> list[dict]:
        system = self._load_system_prompt()
        mem_context = self.memory.load_context()
        if mem_context:
            system += f"\n\n## 跨会话记忆\n{mem_context}"
        context = [{"role": "system", "content": system}]

        # 从当前会话的历史文件恢复
        history_file = self._session_history_path()
        if os.path.exists(history_file):
            try:
                with open(history_file, "r", encoding="utf-8") as f:
                    for line in f:
                        msg = json.loads(line)
                        context.append(msg)
            except Exception:
                pass

        return context

    def _save_context(self, context: list[dict]):
        """将当前上下文持久化到当前会话文件，同时更新会话元信息。"""
        history_file = self._session_history_path()
        msg_count = 0
        try:
            with open(history_file, "w", encoding="utf-8") as f:
                limit = config.MAX_CONTEXT_TOKENS * 3
                current = 0
                for msg in reversed(context):
                    if msg["role"] == "system":
                        continue
                    msg_count += 1
                    save_msg = {"role": msg["role"], "content": msg.get("content", "")}
                    if msg.get("tool_calls"):
                        save_msg["tool_calls"] = msg["tool_calls"]
                    if msg.get("tool_call_id"):
                        save_msg["tool_call_id"] = msg["tool_call_id"]
                    line = json.dumps(save_msg, ensure_ascii=False)
                    current += len(line)
                    if current > limit:
                        break
                    f.write(line + "\n")
        except Exception:
            return

        # 更新会话元信息
        try:
            meta = self._load_meta()
            if self._session_id in meta.get("sessions", {}):
                meta["sessions"][self._session_id]["message_count"] = msg_count
                meta["sessions"][self._session_id]["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_meta(meta)
        except Exception:
            pass

    # ── 上下文裁剪 ──────────────────────────────

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return len(text) // 3

    def _trim_context(self, context: list[dict]):
        """超出 token 限制时丢弃最旧的对话对，保留 system。"""
        if len(context) <= 1:
            return context
        system = context[0]
        rest = context[1:]
        total = sum(self._estimate_tokens(m.get("content", "")) for m in rest)

        while total > config.MAX_CONTEXT_TOKENS and len(rest) > 2:
            removed = rest.pop(0)
            total -= self._estimate_tokens(removed.get("content", ""))
            if rest and rest[0].get("role") == "assistant":
                removed2 = rest.pop(0)
                total -= self._estimate_tokens(removed2.get("content", ""))

        return [system] + rest

    # ── 流式 LLM 调用 ─────────────────────────────

    def _stream_chat(self, context: list[dict]) -> dict:
        """发起流式聊天请求，返回完整 assistant 消息（含 tool_calls 时）。"""
        response = self.client.chat.completions.create(  # type: ignore[arg-type]
            model=self.model,
            messages=context,  # type: ignore[arg-type]
            tools=tools.TOOL_DEFINITIONS,  # type: ignore[arg-type]
            stream=True,
            temperature=config.TEMPERATURE,
            max_tokens=config.MAX_TOKENS,
            extra_body={"enable_thinking": False},
        )

        content = ""
        tool_calls: dict[int, dict] = {}
        print("AI -> ", end="", flush=True)

        for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            if delta.content:
                content += delta.content
                print(delta.content, end="", flush=True)

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls:
                        tool_calls[idx] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                    if tc_delta.id:
                        tool_calls[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tool_calls[idx]["function"]["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            tool_calls[idx]["function"]["arguments"] += tc_delta.function.arguments

        print()  # 流式输出结束换行

        msg: dict = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = [v for _, v in sorted(tool_calls.items())]
        return msg

    # ── 死循环检测 ──────────────────────────────

    def _similarity(self, a: str, b: str) -> float:
        if not a or not b:
            return 0
        clean = lambda s: "".join(c for c in s if c.isalnum()).lower()
        a, b = clean(a), clean(b)
        if not a or not b:
            return 0

        def ngrams(s, n=2):
            return set(s[i:i + n] for i in range(len(s) - n + 1))

        s1, s2 = ngrams(a), ngrams(b)
        if not s1 or not s2:
            return 0
        return len(s1 & s2) / len(s1 | s2)

    @staticmethod
    def _is_tool_placeholder(text: str) -> bool:
        """判断是否为工具调用占位符（不应参与重复检测）"""
        return text.startswith("[调用了工具:") and text.endswith("]")
    
    def _detect_loop(self, iteration: int, recent: deque) -> tuple[bool, str]:
        if iteration >= config.MAX_ITERATIONS:
            return True, f"达到最大迭代次数 ({config.MAX_ITERATIONS})"
        if len(recent) >= config.SIMILAR_RESPONSE_THRESHOLD:
            # 过滤掉工具调用占位符，只检测真实文本响应
            filtered_recent = [r for r in recent if not self._is_tool_placeholder(r)]
            
            # 如果过滤后不足阈值，跳过检测
            if len(filtered_recent) < config.SIMILAR_RESPONSE_THRESHOLD:
                return False, ""
            
            recent_list = filtered_recent[-config.SIMILAR_RESPONSE_THRESHOLD:]
            sims = [self._similarity(recent_list[i], recent_list[i + 1]) for i in range(len(recent_list) - 1)]
            if all(s > 0.8 for s in sims):
                return True, "检测到重复响应模式"
        return False, ""

    # ── 工具执行 ───────────────────────────────

    @staticmethod
    def _execute_tool(tc: dict) -> str:
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"])
        except json.JSONDecodeError as e:
            return f"错误：工具参数 JSON 解析失败 - {e}"

        # 打印工具调用摘要
        safe_args = {k: str(v)[:100] for k, v in args.items()}
        print(f"  🛠️  {name}({json.dumps(safe_args, ensure_ascii=False, indent=2)})")

        try:
            return tools.dispatch(name, **args)
        except Exception as e:
            return f"❌ 工具执行失败 ({name}): {e}"

    # ── 主循环 ──────────────────────────────────

    def _process_turn(self, context: list[dict], user_input: str) -> tuple[str, bool]:
        """处理一条用户输入：LLM 循环 + 工具调用，返回 (最终回复文本, 是否出错)."""
        recent_responses = deque(maxlen=config.SIMILAR_RESPONSE_THRESHOLD + 1)

        if not user_input.strip():
            return "", False

        context.append({"role": "user", "content": user_input})
        iteration = 0
        last_text = ""
        had_error = False

        while True:
            iteration += 1

            loop, reason = self._detect_loop(iteration, recent_responses)
            if loop:
                print(f"\n⚠️  {reason}，强制跳出循环\n")
                context.append({
                    "role": "system",
                    "content": f"系统提示：{reason}。请停止操作并总结。"
                })
                break

            try:
                assistant_msg = self._stream_chat(context)
            except openai.APIError as e:
                print(f"API 错误: {e}")
                had_error = True
                break
            except Exception as e:
                print(f"未知错误: {e}")
                had_error = True
                break

            context.append(assistant_msg)

            text_content = assistant_msg.get("content", "")
            if not text_content:
                tool_names = [tc["function"]["name"] for tc in assistant_msg.get("tool_calls", [])]
                text_content = f"[调用了工具: {', '.join(tool_names)}]"
            else:
                last_text = text_content
            recent_responses.append(text_content)

            tool_calls = assistant_msg.get("tool_calls")
            if tool_calls and len(tool_calls) > 0:
                for tc in tool_calls:
                    result = self._execute_tool(tc)
                    # 打印工具结果摘要给用户
                    preview = result.strip()[:300]
                    if len(result) > 300:
                        preview += "  ..."
                    print(f"  📋  {preview}")

                    if len(result) > 2000:
                        result = result[:2000] + f"\n... (已截断，原文 {len(result)} 字符)"
                    context.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    })
                context = self._trim_context(context)
                continue

            break

        if iteration > 1:
            print(f"📊 本次交互共迭代 {iteration} 次\n")

        return last_text, had_error

    def _auto_drive(self, context: list[dict]):
        """自动推进：有 pending 任务时持续驱动 agent 执行，直到全部完成或达到上限。"""
        max_auto = 10
        for _ in range(max_auto):
            pending = tools.get_pending_tasks()
            if not pending:
                return

            task = pending[0]
            prompt = f"请继续执行待办任务 #{task['id']}: {task['subject']}"
            if task.get("description"):
                prompt += f"\n\n任务详情:\n{task['description']}"

            print(f"\n🔄 [自动] 任务 #{task['id']}: {task['subject']}")
            _, had_error = self._process_turn(context, prompt)
            tools.save_tasks()

            if had_error:
                print(f"⏹️  任务 #{task['id']} 执行异常，停止自动推进")
                break

        remaining = len(tools.get_pending_tasks())
        if remaining:
            print(f"⏸️  自动推进暂停，剩余 {remaining} 个待办任务需手动处理")

    # ── 退出处理 ──────────────────────────────

    # ── 会话关闭 ──────────────────────────────

    def _close_session(self, context: list[dict]) -> str:
        """关闭当前会话：保存上下文、生成总结、重命名文件。不修改 context。返回总结文本。"""
        self._save_context(context)

        # 用独立上下文请求 LLM 总结，不污染原 context
        summary_context = context + [
            {"role": "user", "content": "请总结一下，给这次对话起一个5到10个汉字的名字。不要添加任何多余的文字。"}
        ]
        try:
            assistant_msg = self._stream_chat(summary_context)
            summary = assistant_msg.get("content", "").strip()
        except Exception:
            summary = ""

        # 回退：API 失败时取首条用户消息
        if not summary:
            for m in context:
                if m["role"] == "user":
                    text = m.get("content", "").strip()
                    if text:
                        summary = text.split("\n")[0].strip()[:50]
                        break
        if not summary:
            summary = "empty_session"

        # 重命名会话文件：添加总结+时间戳防止重叠
        old_path = self._session_history_path()
        new_path = old_path
        if os.path.exists(old_path):
            safe = re.sub(r'[^\w]+', '_', summary)[:40].strip('_')
            ts = datetime.now().strftime("%H%M%S")
            new_name = f"{self._session_id}_{safe}_{ts}.jsonl"
            new_path = os.path.join(config.SESSIONS_DIR, new_name)
            os.rename(old_path, new_path)

        # 更新 meta
        meta = self._load_meta()
        if self._session_id in meta.get("sessions", {}):
            meta["sessions"][self._session_id]["summary"] = summary
            meta["sessions"][self._session_id]["file"] = os.path.basename(new_path)
            meta["sessions"][self._session_id]["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._save_meta(meta)

        return summary

    def _shutdown(self, context: list[dict]):
        """退出处理：关闭会话 → 显示统计信息。"""
        summary = self._close_session(context)

        meta = self._load_meta()
        info = meta.get("sessions", {}).get(self._session_id, {})
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

        # TUI 展示
        W = 48
        sep = "─" * W
        def tb(text: str) -> str:
            return f"║  {text:<{W+4}s}║"
        print(f"\n╔{sep}╗")
        print(tb("📂  会话结束"))
        print(f"║{sep}║")
        print(tb(f"总结: {summary}"))
        new_path = self._session_history_path()
        print(tb(f"文件: {os.path.basename(new_path)}"))
        print(f"║{sep}║")
        print(tb("📊  统计信息"))
        print(f"║{sep}║")
        print(tb(f"模型: {self.model}"))
        print(tb(f"消息: {msg_count} 条"))
        print(tb(f"创建: {created}"))
        if duration:
            print(tb(f"耗时: {duration}"))
        print(f"╚{sep}╝")
        print("\n👋 再见！")

    def run(self):
        """交互式 REPL 模式：循环等待用户输入。"""
        context = self._build_context()

        print(f"🤖 Five Pebbels 已启动 (模型: {self.model})\n")

        try:
            while True:
                try:
                    user_input = self._get_user_input()
                except EOFError:
                    print()
                    break
                except KeyboardInterrupt:
                    print("已中断!\n如需退出可使用 '/exit' 命令退出")
                    continue
                try:
                    if not user_input.strip():
                        continue

                    if self._handle_command(user_input, context):
                        continue

                    self._process_turn(context, user_input)
                    self._auto_drive(context)
                    self._save_context(context)
                except KeyboardInterrupt:
                    print("已中断!\n如需退出可使用 '/exit' 命令退出")
        except Exception as e:
            print(f"\n❌ 程序异常退出: {e}")
        finally:
            try:
                self._shutdown(context)
            except Exception:
                pass
            tools.save_tasks()

    def run_once(self, user_input: str) -> str:
        """单次查询模式：处理一条输入就退出，返回最终回复文本。"""
        context = self._build_context()
        result, _ = self._process_turn(context, user_input)
        self._auto_drive(context)
        self._save_context(context)
        return result


def main():
    parser = argparse.ArgumentParser(description="Five Pebbels AI Agent")
    parser.add_argument("query", nargs="?", help="直接传入查询（非交互模式）")
    parser.add_argument("-r", "--resume", action="store_true", 
                        help="继续最近结束的会话")
    args = parser.parse_args()

    if args.resume:
        # 恢复模式：不创建新会话，直接切换到最近的会话
        agent = Agent(resume_mode=True)
        agent._cmd_resume("", [])
    else:
        agent = Agent()
    
    if args.query:
        agent.run_once(args.query)
    elif not sys.stdin.isatty():
        query = sys.stdin.read().strip()
        if query:
            agent.run_once(query)
        else:
            agent.run()
    else:
        agent.run()


if __name__ == "__main__":
    main()
