# fp-terminal — 五块卵石终端 REPL

[![PyPI](https://img.shields.io/pypi/v/fp-terminal)](https://pypi.org/project/fp-terminal/)
[![Python](https://img.shields.io/pypi/pyversions/fp-terminal)](https://pypi.org/project/fp-terminal/)
[![License](https://img.shields.io/pypi/l/fp-terminal)](LICENSE)

## 简介

**fp-terminal** 是五块卵石（Five Pebbles）Agent 框架的交互式命令行界面。它基于 `prompt-toolkit` 构建，提供了语法高亮、自动补全、多行编辑、历史记录等现代终端体验。

> 通常通过主包 `fp` 安装，无需单独安装。

---

## 特性

- **REPL 交互模式** — 实时与 Agent 对话，流式输出响应
- **命令系统** — 内置 `/` 命令用于控制会话、加载插件、切换模型等
- **语法高亮** — 代码块自动识别并着色
- **自动补全** — 命令与参数 Tab 补全
- **多行编辑** — 支持粘贴多行文本或代码
- **会话历史** — 对话历史持久化存储，跨会话可回溯
- **多彩输出** — 基于 `rich` 的渲染引擎

---

## 安装

```bash
pip install fp-terminal
```

要求 Python >= 3.11。

> **Windows 用户**：推荐安装 [Git for Windows](https://git-scm.com)（安装时勾选"Git Bash"并添加到 PATH），以获得完整的 Unix 命令支持。未安装时会自动降级到 cmd.exe，仅支持 Windows 原生命令。

---

## 快速使用

### 启动 REPL

```bash
fp
```

或显式调用：

```bash
python -m fp_cli
```

启动后进入交互模式：

```
╭─ Five Pebbles ───────────────────────────╮
│ 输入 /help 查看可用命令                   │
│ 按 Ctrl+D 或输入 /exit 退出               │
╰──────────────────────────────────────────╯

🤖 你好！我是五块卵石，请告诉我你的问题。
```

### 开始对话

```
>> 请用 Python 写一个快速排序
# 直接输入即发送消息，Agent 会流式输出回复

>> /save session-quicksort
# 保存当前会话

>> /load session-quicksort
# 恢复历史会话
```

---

## 命令列表

| 命令 | 别名 | 说明 |
|------|------|------|
| `/help` | `/h` | 显示帮助信息 |
| `/exit` | `/q`, `/quit` | 退出 REPL |
| `/clear` | `/c` | 清屏 |
| `/save <name>` | — | 保存当前会话 |
| `/load <name>` | — | 加载指定会话 |
| `/list` | `/ls` | 列出所有会话 |
| `/delete <name>` | `/rm` | 删除指定会话 |
| `/model <name>` | — | 切换 LLM 模型 |
| `/plugin list` | — | 列出已加载的插件 |
| `/plugin load <name>` | — | 动态加载插件 |
| `/tool list` | — | 列出可用工具 |
| `/env` | — | 查看当前环境配置 |
| `/config <key>=<value>` | — | 动态修改配置 |
| `/reset` | — | 重置当前会话上下文 |
| `/export <format>` | — | 导出会话（json/md） |

---

## 快捷键

| 快捷键 | 功能 |
|--------|------|
| `Tab` | 自动补全命令/参数 |
| `Ctrl+C` | 中断当前输出 |
| `Ctrl+D` | 退出 REPL |
| `Ctrl+L` | 清屏 |
| `Ctrl+R` | 反向搜索历史 |
| `Up/Down` | 浏览历史命令 |
| `Shift+Enter` | 换行（多行输入） |
| `Ctrl+W` | 删除前一个词 |
| `Ctrl+U` | 删除整行 |
| `Ctrl+K` | 删除光标至行尾 |

---

## 配置

通过命令行标志自定义启动行为：

```bash
# 指定配置文件
fp --config ./my-config.yaml

# 指定初始模型
fp --model claude-3-opus

# 静默模式（跳过启动横幅）
fp --quiet

# 调试模式
fp --debug
```

配置文件示例（YAML）：

```yaml
cli:
  theme: dark          # dark / light
  multi_line: true     # 启用多行编辑
  max_history: 1000    # 历史记录条数
  stream: true         # 流式输出
```

---

## 依赖

| 依赖 | 版本 | 用途 |
|------|------|------|
| `fp-core` | >= 0.1.0 | Agent 核心引擎 |
| `prompt-toolkit` | >= 3.0.40 | 终端交互框架 |

---

## 许可

MIT © zpb
