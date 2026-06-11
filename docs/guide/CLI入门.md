# CLI 入门

Five Pebbles 的主交互方式是通过命令行界面（CLI）。本文档涵盖基本用法、命令行参数、快捷键和中断处理。

---

## 基本用法

```bash
# 进入交互模式
python3 cli.py
```

交互模式下，终端显示提示符 `(Agent) > `，用户可直接输入文本消息与 AI 对话，或以 `/` 开头的斜杠命令控制系统行为。

---

## 命令行参数

| 参数 | 完整形式 | 说明 |
|------|---------|------|
| `-m <msg>` | `--message <msg>` | 单次消息模式。发送一条消息后直接退出，不进入交互循环 |
| `-r` | `--resume` | 恢复最新会话。不加参数值则自动恢复最新的历史会话 |
| `-r <SID>` | `--resume <SID>` | 恢复指定 ID 的会话 |
| `--init` | — | 初始化配置文件。生成默认 `config.json` 后退出 |

---

## 示例

### 1. 交互模式

```bash
python3 cli.py
```

进入交互界面，显示启动 Logo 和提示信息，用户可连续输入消息。

### 2. 单次消息模式

```bash
python3 cli.py -m "你好"
```

发送消息后自动退出，适合脚本调用或快速测试。

### 3. 恢复最新会话

```bash
python3 cli.py -r
```

或完整形式：

```bash
python3 cli.py --resume
```

自动加载最近一次保存的会话上下文，继续之前的对话。

### 4. 恢复指定会话

```bash
python3 cli.py -r session_abc123
```

### 5. 初始化配置文件

```bash
python3 cli.py --init
```

在项目根目录生成默认 `config.json`。

---

## 快捷键

| 快捷键 | 功能 |
|--------|------|
| `Tab` | 补全。输入 `/` 后按 Tab 弹出补全菜单，再次按 Tab 确认选中项 |
| `Ctrl+C` | 中断当前操作。LLM 思考中中断则保留已生成内容；工具执行中中断则标记当前工具失败、其余未执行 |
| `Ctrl+D` | 退出程序，等同于输入 `exit` 命令。触发正常退出流程（生成总结、统计面板、保存会话） |
| `↑` / `↓` | 浏览输入历史（基于 `prompt_toolkit` 的 `FileHistory`） |
| `Meta+Enter` | 打开外部编辑器编辑当前输入内容（`prompt_toolkit` 内置功能） |

---

## Tab 补全详解

在输入 `/` 开始输入斜杠命令时，按下 `Tab` 键触发自动补全：

1. 按下 `Tab` — 弹出补全菜单，显示所有匹配的命令名及其描述
2. 再次按下 `Tab` — 确认当前选中的补全项，插入到输入中

补全列表从命令系统和技能系统动态加载，确保始终与实际可用内容同步。每个补全项附带描述信息（`display_meta`），帮助快速了解功能。

---

## Ctrl+C 中断处理

Five Pebbles 采用双保险中断机制，确保在任意状态下中断操作而不会导致数据损坏。

### 中断机制的层级

**层级一（Unix 主线程）**：通过 `signal.signal(signal.SIGINT, _raw_sigint_handler)` 安装纯 C 级信号处理器。信号到达时调用 `asyncio.all_tasks().cancel()` 注入 `CancelledError`，由各协程的 `try/except` 优雅捕获。

**层级二（跨平台回退）**：设置全局标志 `_interrupted_flag = True`。此标志在 Windows 等平台上作为安全网——`_check_interrupted()` 方法在 Agent 主循环的每次迭代中检查该标志，若置位则抛出 `CancelledError`。

### LLM 思考中中断

当 LLM 正在生成回复流时按下 `Ctrl+C`，中断会捕获已经生成的内容并保留到上下文中。LLM 回复中的 `tool_calls` 会被标记为中断状态，已生成的文本内容不会被丢弃。

```python
# 中断后的处理逻辑
if interrupted:
    display.info("⏹️ 已中断（保留了已生成的内容）")
    break
```

### 工具执行中中断

当 Agent 正在依次执行多个工具调用时按下 `Ctrl+C`：

- 当前正在执行的工具被标记为"工具调用失败：用户中断"
- 其余尚未执行的工具均标记为"未执行（工具调用失败：用户中断）"
- 所有工具调用信息保留在上下文中，Agent 不会丢失状态

```python
except (KeyboardInterrupt, asyncio.CancelledError):
    self._conv.add_tool_message(tc["id"], "工具调用失败：用户中断")
    for remaining in tool_calls[i + 1 :]:
        self._conv.add_tool_message(remaining["id"], "未执行（工具调用失败：用户中断）")
    break
```
