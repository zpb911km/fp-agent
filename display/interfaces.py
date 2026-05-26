"""
Display 抽象接口 — 所有 TUI 渲染方法在此声明。

逻辑层通过此接口与显示层交互，不依赖具体实现。
每个方法对应一种 UI 行为，力求方法签名最小化参数。
"""

from abc import ABC, abstractmethod
from typing import Any


class Display(ABC):
    """显示层抽象基类 — 逻辑层与显示层的合同"""

    # ── 生命周期 ────────────────────────────────

    @abstractmethod
    def initialize(self) -> None:
        """初始化显示系统（清屏、准备资源等）"""
        ...

    @abstractmethod
    def shutdown(self) -> None:
        """关闭显示系统（恢复终端、释放资源等）"""
        ...

    # ── 流式 AI 输出 ────────────────────────────

    @abstractmethod
    def on_ai_thinking_start(self) -> None:
        """AI 开始思考（reasoning_content 开始输出）"""
        ...

    @abstractmethod
    def on_ai_thinking_chunk(self, text: str) -> None:
        """AI 思考内容增量"""
        ...

    @abstractmethod
    def on_ai_thinking_end(self) -> None:
        """AI 思考结束，准备输出正文"""
        ...

    @abstractmethod
    def on_ai_response_start(self) -> None:
        """AI 开始输出回复"""
        ...

    @abstractmethod
    def on_ai_response_chunk(self, text: str) -> None:
        """AI 回复增量（已渲染为 Markdown → ANSI）"""
        ...

    @abstractmethod
    def on_ai_response_end(self) -> None:
        """AI 流式输出结束"""
        ...

    # ── 工具调用显示 ────────────────────────────

    @abstractmethod
    def on_tool_call(self, name: str, args: dict) -> None:
        """工具被调用时触发
        
        Args:
            name: 工具名称
            args: 参数字典（值已截断至 100 字符）
        """
        ...

    @abstractmethod
    def on_tool_result(self, preview: str) -> None:
        """工具执行结果预览
        
        Args:
            preview: 结果预览（已截断至 300 字符）
        """
        ...

    # ── 系统 / 状态消息 ─────────────────────────

    @abstractmethod
    def on_info(self, message: str) -> None:
        """一般信息"""
        ...

    @abstractmethod
    def on_warning(self, message: str) -> None:
        """警告信息"""
        ...

    @abstractmethod
    def on_error(self, message: str) -> None:
        """错误信息"""
        ...

    @abstractmethod
    def on_success(self, message: str) -> None:
        """成功信息"""
        ...

    @abstractmethod
    def on_stats(self, iteration_count: int) -> None:
        """交互迭代统计"""
        ...

    # ── 自动推进显示 ────────────────────────────

    @abstractmethod
    def on_auto_task_start(self, task_id: int, subject: str) -> None:
        """自动任务开始执行"""
        ...

    @abstractmethod
    def on_auto_task_error(self, task_id: int) -> None:
        """自动任务执行异常"""
        ...

    @abstractmethod
    def on_auto_paused(self, remaining: int) -> None:
        """自动推进暂停"""
        ...

    # ── 启动 / 退出界面 ─────────────────────────

    @abstractmethod
    def on_startup(self, model_name: str) -> None:
        """程序启动横幅"""
        ...

    @abstractmethod
    def on_shutdown(self, summary: str, stats: dict[str, Any]) -> None:
        """程序退出统计面板
        
        Args:
            summary: 会话总结标题
            stats: 统计字典（model, msg_count, created, duration, file 等）
        """
        ...

    # ── 命令响应 ────────────────────────────────

    @abstractmethod
    def show_help(self, commands: dict[str, str]) -> None:
        """显示帮助信息"""
        ...

    @abstractmethod
    def show_skills(self, skills_text: str) -> None:
        """显示技能列表"""
        ...

    @abstractmethod
    def show_model_config(self, model: str, temperature: float, max_tokens: int) -> None:
        """显示模型配置"""
        ...

    @abstractmethod
    def show_session_info(self, session_id: str, info: dict) -> None:
        """显示当前会话信息"""
        ...

    # ── 加载 / 进度指示 ─────────────────────────

    @abstractmethod
    def on_loading_plugin(self, plugin_name: str, status: str) -> None:
        """插件加载状态
        
        Args:
            plugin_name: 名称
            status: 'loaded' | 'skipped' | 'failed' | 'dir_missing'
        """
        ...

    @abstractmethod
    def on_loading_skill(self, skill_name: str, status: str) -> None:
        """技能加载状态
        
        Args:
            skill_name: 名称
            status: 'loaded' | 'failed'
        """
        ...

    @abstractmethod
    def on_loading_task(self, task_file: str, status: str, count: int = 0) -> None:
        """任务文件加载状态"""
        ...

    @abstractmethod
    def on_task_saved(self, count: int) -> None:
        """任务已保存"""
        ...

    @abstractmethod
    def on_reloading_skills(self) -> None:
        """技能热重载中"""
        ...

    # ── 用户输入 ────────────────────────────────

    @abstractmethod
    def prompt_user(self) -> str:
        """获取用户输入（显示提示符并读取一行）"""
        ...

    @abstractmethod
    def on_interrupt(self) -> None:
        """用户中断当前操作（Ctrl+C）"""
        ...

    @abstractmethod
    def on_eof(self) -> None:
        """用户输入结束（Ctrl+D）"""
        ...
