"""
ConsoleDisplay — 基于 ANSI 终端的 Display 实现

将 agent.py 中所有分散的 print/ANSI 逻辑集中于此，
保持与原始终端行为一致，并提供统一的扩展点。
"""

import sys
import re
from typing import Optional

from .interfaces import Display
from .tokens import Style, Fg, Border, icons
from .widgets import panel, badge


# ── Markdown → ANSI 渲染器 ─────────────────────

def render_markdown(text: str) -> str:
    """将 Markdown 语法转换为 ANSI 终端颜色代码。
    
    支持：**粗体**、*斜体*、`行内代码`、代码块、列表标记。
    不依赖第三方库，纯正则实现。
    """
    # 1. 代码块（```...```）→ 使用青色
    text = re.sub(
        r'```(\w*)\n(.*?)```',
        lambda m: f'{Fg.CYAN}```{m.group(1)}\n{m.group(2)}```{Style.RESET}',
        text, flags=re.DOTALL
    )
    
    # 2. 行内代码（`code`）→ 青色
    text = re.sub(r'`([^`]+)`', rf'{Fg.CYAN}`\1`{Style.RESET}', text)
    
    # 3. **粗体** → 高亮加粗
    text = re.sub(r'\*\*(.+?)\*\*', rf'{Style.BOLD}\1{Style.RESET}', text)
    
    # 4. *斜体* → 暗淡模式（先处理避免与 ** 冲突）
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', rf'{Style.DIM}\1{Style.RESET}', text)
    
    # 5. 列表标记 - 和 `>` 引用 → 黄色
    text = re.sub(r'^(- |> |>)(.*)', rf'{Fg.YELLOW}\1{Style.RESET}\2', text, flags=re.MULTILINE)
    
    return text


# ── ConsoleDisplay 实现 ────────────────────────

class ConsoleDisplay(Display):
    """基于 ANSI 转义序列的控制台显示实现。
    
    行为与原始 agent.py 中的 print() 调用完全一致，
    但将所有输出逻辑集中于此，方便后续替换为 curses/ncurses TUI。
    """

    def __init__(self, markdown_enabled: bool = True):
        self.markdown_enabled = markdown_enabled

    # ── 生命周期 ────────────────────────────────

    def initialize(self) -> None:
        pass  # 终端模式无需特殊初始化

    def shutdown(self) -> None:
        pass

    # ── 流式 AI 输出 ────────────────────────────

    def on_ai_thinking_start(self) -> None:
        sys.stdout.write(f"{Style.DIM}[思考]{Style.RESET}")
        sys.stdout.flush()

    def on_ai_thinking_chunk(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def on_ai_thinking_end(self) -> None:
        sys.stdout.write("\n")
        sys.stdout.flush()

    def on_ai_response_start(self) -> None:
        sys.stdout.write("AI -> ")
        sys.stdout.flush()

    def on_ai_response_chunk(self, text: str) -> None:
        rendered = render_markdown(text) if self.markdown_enabled else text
        sys.stdout.write(rendered)
        sys.stdout.flush()

    def on_ai_response_end(self) -> None:
        sys.stdout.write(f"{Style.RESET}\n")
        sys.stdout.flush()

    # ── 工具调用显示 ────────────────────────────

    def on_tool_call(self, name: str, args: dict) -> None:
        import json
        safe_args = {k: str(v)[:100] for k, v in args.items()}
        print(f"  🛠️  {name}({json.dumps(safe_args, ensure_ascii=False, indent=2)})")

    def on_tool_result(self, preview: str) -> None:
        print(f"  📋  {preview}")

    # ── 系统 / 状态消息 ─────────────────────────

    def on_info(self, message: str) -> None:
        print(f"ℹ️  {message}")

    def on_warning(self, message: str) -> None:
        print(f"⚠️  {message}")

    def on_error(self, message: str) -> None:
        print(f"❌ {message}")

    def on_success(self, message: str) -> None:
        print(f"✅  {message}")

    def on_stats(self, iteration_count: int) -> None:
        print(f"📊 本次交互共迭代 {iteration_count} 次\n")

    # ── 自动推进显示 ────────────────────────────

    def on_auto_task_start(self, task_id: int, subject: str) -> None:
        print(f"\n🔄 [自动] 任务 #{task_id}: {subject}")

    def on_auto_task_error(self, task_id: int) -> None:
        print(f"⏹️  任务 #{task_id} 执行异常，停止自动推进")

    def on_auto_paused(self, remaining: int) -> None:
        print(f"⏸️  自动推进暂停，剩余 {remaining} 个待办任务需手动处理")

    # ── 启动 / 退出界面 ─────────────────────────

    def on_startup(self, model_name: str) -> None:
        print(f"🤖 Five Pebbels 已启动 (模型: {model_name})\n")

    def on_shutdown(self, summary: str, stats: dict) -> None:
        W = 48
        sep = "─" * W
        
        def tb(text: str) -> str:
            return f"║  {text:<{W+4}s}║"

        print(f"\n╔{sep}╗")
        print(tb("📂  会话结束"))
        print(f"║{sep}║")
        print(tb(f"总结: {summary}"))
        if "file" in stats:
            print(tb(f"文件: {stats['file']}"))
        print(f"║{sep}║")
        print(tb("📊  统计信息"))
        print(f"║{sep}║")
        if "model" in stats:
            print(tb(f"模型: {stats['model']}"))
        if "msg_count" in stats:
            print(tb(f"消息: {stats['msg_count']} 条"))
        if "created" in stats:
            print(tb(f"创建: {stats['created']}"))
        if "duration" in stats and stats["duration"]:
            print(tb(f"耗时: {stats['duration']}"))
        print(f"╚{sep}╝")
        print("\n👋 再见！")

    # ── 命令响应 ────────────────────────────────

    def show_help(self, commands: dict[str, str]) -> None:
        print("可用命令:")
        for cmd, desc in sorted(commands.items()):
            print(f"  {cmd:12s}  {desc}")

    def show_skills(self, skills_text: str) -> None:
        print(skills_text)

    def show_model_config(self, model: str, temperature: float, max_tokens: int) -> None:
        print(f"模型:   {model}")
        print(f"温度:   {temperature}")
        print(f"最大 Token: {max_tokens}")
        print(f"会话目录: （由 config 提供）")

    def show_session_info(self, session_id: str, info: dict) -> None:
        # 由 agent 的 _cmd_session 自行组装显示
        pass

    # ── 加载 / 进度指示 ─────────────────────────

    def on_loading_plugin(self, plugin_name: str, status: str) -> None:
        msg_map = {
            "loaded":       f"✅ 已加载插件：{plugin_name}",
            "skipped":      f"⚠️ 插件 {plugin_name} 缺少必要接口，跳过",
            "failed":       f"❌ 加载插件 {plugin_name} 失败",
            "dir_missing":  "⚠️ 插件目录不存在",
        }
        print(msg_map.get(status, status))

    def on_loading_skill(self, skill_name: str, status: str) -> None:
        msg_map = {
            "loaded": f"✅ 已加载技能：{skill_name}",
            "failed": f"❌ 加载技能 {skill_name} 失败",
        }
        print(msg_map.get(status, status))

    def on_loading_task(self, task_file: str, status: str, count: int = 0) -> None:
        if status == "created":
            print(f"✅ 已创建任务文件：{task_file}")
        elif status == "exists":
            print(f"📂 任务文件已存在：{task_file}")
        elif status == "loaded":
            print(f"📋 已加载 {count} 个任务")
        elif status == "failed":
            print(f"⚠️ 加载任务失败：{task_file}")

    def on_task_saved(self, count: int) -> None:
        print(f"💾 任务已保存 ({count} 个任务)")

    def on_reloading_skills(self) -> None:
        print("🔄 正在重新加载技能...")

    # ── 用户输入 ────────────────────────────────

    def prompt_user(self) -> str:
        try:
            return input().strip()
        except EOFError:
            raise
        except KeyboardInterrupt:
            raise

    def on_interrupt(self) -> None:
        print("已中断!\n如需退出可使用 '/exit' 命令退出")

    def on_eof(self) -> None:
        print()
