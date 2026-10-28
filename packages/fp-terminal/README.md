# fp-terminal — FP 终端 REPL

[![PyPI](https://img.shields.io/pypi/v/fp-terminal)](https://pypi.org/project/fp-terminal/)
[![Python](https://img.shields.io/pypi/pyversions/fp-terminal)](https://pypi.org/project/fp-terminal/)
[![License](https://img.shields.io/pypi/l/fp-terminal)](LICENSE)

## 简介

**fp-terminal** 是FPAgent 框架的交互式命令行界面。它基于 `prompt-toolkit` 构建，提供了语法高亮、自动补全、多行编辑、历史记录等现代终端体验。

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
╭─ FP ───────────────────────────╮
│ 输入 /help 查看可用命令                   │
│ 按 Ctrl+D 或输入 /exit 退出               │
╰──────────────────────────────────────────╯

🤖 你好！我是 FP，请告诉我你的问题。
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

### TUI 模式

TUI 里**所有操作都可纯键盘完成**——鼠标不可用的终端（如 Windows 传统控制台）
同样能全功能使用。按 `F1` 或 `Ctrl+/` 查看当前生效键位。

默认键位取**保守集**，并刻意做到**不依赖功能键**：

- **每个核心操作都有非功能键通路**。`F1-F12`、`PgUp/PgDn`、`Home/End` 在 60%
  机械键盘上要 `Fn` 组合、在 Mac 上是亮度/音量层、部分终端还会截获——所以它们
  只作加速器（`F1`、`PgUp/PgDn` 仍然可用），唯一入口一律是 `Ctrl+字母`。
- 避开 `Alt+方向键`（Windows conhost 需终端配合）和 `Ctrl+Shift+字母`（非 kitty
  键盘协议下会退化成 `Ctrl+字母`）。
- `Ctrl+/` 在终端里与 `Ctrl+_` 是**同一个字节**（`0x1F`），Textual 解析为
  `ctrl+underscore`。改键面板和配置文件都接受 `ctrl+/` 写法，显示时也还原成
  `Ctrl+/`。

| 快捷键 | 功能 |
|--------|------|
| `Shift+Enter` | 发送消息 |
| `Enter` | 输入框内换行（普通可改键动作 `insert_newline`，改走后 `Enter` 即让位） |
| `Ctrl+Q` | 退出 |
| `Ctrl+C` | 复制选区 / 打断运行（空闲且无选区时只提示，不退出） |
| `F1` / `Ctrl+/` | 快捷键总览（也见 `/help`） |
| `F1` / `Esc` | 总览面板内关闭 |
| `PgUp` / `PgDn` 或 `Ctrl+U` / `Ctrl+D` | 消息区上 / 下翻页 |
| `Ctrl+Home` / `Ctrl+End` | 跳到最早 / 最新消息 |
| `Ctrl+N` / `Ctrl+P` | 聚焦下一张 / 上一张卡片（工具、思考卡） |
| `Ctrl+O` | 开合当前卡片（无聚焦卡片时作用于最后一张） |
| `Ctrl+E` | 全部展开 / 全部折叠 |
| `Ctrl+G` | 会话统计 |
| `Ctrl+T` | 外观设置 |
| `Ctrl+K` | 键位设置（也可 `fp --keys`） |
| `Tab` | 命令/路径补全 |

消息区的键是**优先级绑定**：焦点停在输入框里也照常生效，不必先 `Tab` 出去。

#### 外观面板（`Ctrl+T`，再按一次关闭）

| 操作 | 键 |
|---|---|
| 上 / 下一分页 | `←` / `→`（五个分页循环） |
| 选中字段 | `↑` / `↓`、`PgUp` / `PgDn` |
| 编辑 / 切换开关 | `Enter`（主题页 = 选定并落盘；字段页 = 打开编辑框） |
| 恢复当前项默认 | `R`（编辑框留空同效） |
| 关闭 | `Esc`（放弃未落盘的主题预览） |

`Tab` / `Shift+Tab` 在面板内移动焦点；主屏输入框里的 `Tab` 仍是命令补全。
80x24 小终端下面板自动收缩，标题与操作提示不会被裁到屏幕外。

#### 自定义键位

`config.json` 的 `tui.keys`，键名是动作 id（见 `F1` 面板，或
`src/fp_cli/tui_actions.py` 的 `ACTIONS`）：

```json
{
  "tui": {
    "keys": {
      "card_next": "ctrl+down",
      "help": "f2",
      "scroll_page_up": ["pageup", "ctrl+b"]
    }
  }
}
```

值是键串或键串数组（数组 = 等价别名）。非法项**逐条忽略并在启动时提示原因**，
不会因为一个手滑的配置项让 TUI 起不来。

#### 图形化改键（推荐）

按 `Ctrl+K`，或在 shell 里 `fp --keys`（启动即开面板）：

| 操作 | 键 |
|---|---|
| 选中动作 | `↑` / `↓` |
| 改键（进入捕获） | `Enter` → 按新键 |
| 恢复当前项默认 | `R` |
| 全部恢复默认 | `Ctrl+R` |
| 关闭 | `Esc` |

`fp --keys` 不经过 TUI 内任何键位绑定 —— **键位配坏、或终端认不出 `Ctrl+K`/`F1`
时，这是唯一可靠的兜底入口**；在非交互环境（管道、无 TTY）它直接打印键位表：

```bash
fp --keys          # TUI 里打开面板 / 无 TTY 时打印表格
fp --keys | less   # 查看全部键位
```

> 外壳**不占用 `/` 斜杠命名空间**——那是 agent 与插件的命令注册表（`/help`、
> `/reload` 等都在里面）。外壳若静默劫持同名命令，插件永远轮不到，用户还得
> 不到任何提示。

改键**立即生效**并写回 `config.json` 的 `tui.keys`。捕获到已被同作用域其他动作
占用的键会拒绝并提示是谁占着，避免绑出一个静默失效的键。

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
