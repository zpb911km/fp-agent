# fp-terminal — FP 的终端界面

[![PyPI](https://img.shields.io/pypi/v/fp-terminal.svg)](https://pypi.org/project/fp-terminal/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](../../LICENSE)

> **Textual** 打造的终端 UI：流式对话、可折叠卡片、多行输入、补全浮层、键位可配置。
> 它是 `fp` 命令的默认前端，通常随 `fp-agent` 一并安装，无需单独引入。

## 安装

```bash
pip install fp-agent       # 推荐：`fp` 命令全家桶（fp-core + fp-terminal）
pip install fp-terminal    # 只装终端界面（fp-core 会被一并拉入）
```

要求 Python >= 3.11。

## 启动

```bash
fp                    # 直接进入 TUI
fp -r [SESSION_ID]    # 恢复历史会话再进 TUI
fp --keys             # 启动即打开键位面板（无 TTY 时打印键位表后退出）
```

`fp` 默认走 TUI（L0）。下列任一条件成立时**自动降级**为经典 REPL（L1：裸 `input`，
无补全、无历史），并在首行提示原因：

| 条件 | 降级原因 |
|------|---------|
| `FP_NO_TUI=1` | 显式关闭 TUI |
| `FP_IS_SUBAGENT=1` | subagent 子进程 |
| stdin 或 stdout 不是 TTY | 管道 / 重定向 |
| `TERM=dumb` | 终端不支持交互渲染 |
| 未安装 `textual` | 依赖缺失 |

`fp -m "..."`（单次消息）与 `fp --headless`（无头驻留）本就不进交互前端，走 L2 打印通路。

## 交互

输入框是**多行**的，所以「换行」与「发送」是两个键：

| 按键 | 动作 |
|------|------|
| `Shift+Enter` | **发送**（等价点击右侧发送按钮） |
| `Enter` | 输入框内**换行**（动作 `insert_newline`，在 `Ctrl+K` 面板改键后 `Enter` 即让位） |
| `Tab` | 补全：`/` 命令与路径，插入当前选中候选 |
| `↑` / `↓` | 光标未到行边界时移动光标；到首/末行时翻**输入历史**；补全浮层打开时选候选 |
| `Esc` | 关闭补全浮层 |
| `Ctrl+C` | 复制当前选区（OSC52 写入系统剪贴板）；无选区只提示，**不退出** |
| `Ctrl+Q` | **退出**（走正常收尾：生成摘要、统计面板、保存会话） |
| `Ctrl+K` | 键位设置面板开关（再按一次关闭） |

- **打断**（`interrupt`）默认**不绑键**：`Ctrl+C` 已归复制，给它别的键等于替用户拍板。要打断请自行设键。
- 其余动作——消息区翻页、卡片导航与开合、统计、外观、输入框内编辑——出厂**一律未绑定**，在 `Ctrl+K` 面板里按自己的手感分配。TextArea 的原生编辑键（`Ctrl+A/E/U/K/W`、`Ctrl+Z/Y`、`Ctrl+V`…）始终生效。
- 一个动作只绑**一个**键。覆盖写在 `~/.config/fp/config.json` 的 `tui.keys`（动作 id → 键串）；非法项逐条忽略并提示原因，坏配置不会让 TUI 起不来。

## 内置命令

16 条内置命令，均以 `/` 开头并支持 `Tab` 补全：

| 命令 | 别名 | 说明 |
|------|------|------|
| `/help` | `/?` | 显示可用命令列表 |
| `/exit` | `/quit` | 正常退出（摘要 + 统计面板 + 保存会话） |
| `/exit!` | — | 核弹级退出：删除当前会话、不留痕迹 |
| `/clear` | — | 清空当前会话 |
| `/new` | `/fresh` | 新建空白会话（先保存当前会话） |
| `/back` | — | 回退到对话的某个历史时刻（`/back list` 列出，`/back <N>` 回退） |
| `/history` | — | 查看当前对话历史 |
| `/fork` | — | 基于当前上下文新建会话 |
| `/compact` | — | 压缩对话历史 |
| `/sc` | — | 修剪历史连通块：`list` / `N` / `-d` 退化 / `-c` 裁剪 / `-r` 提炼 |
| `/task` | — | 任务图控制台：`list` `show` `new` `approve` `answer` `pause` 等 |
| `/resume` | — | 切换 / 删除 / 清理历史会话 |
| `/option` | `/opt` `/op` `/ext` `/extension` `/extensions` | 统一管理 commands / plugins / tools 三种扩展 |
| `/reload` | `/rl` `/hotreload` | 热重启进程（落盘会话 → 原命令重启 → 自动恢复） |
| `/token` | `/tokens` `/usage` | 显示当前会话 token 消耗统计 |
| `/session` | — | 显示当前会话信息 |

其中 `/sc` 与 `/task` 由插件在启动时注入注册表，其余以命令文件自动发现；用户级扩展命令（插件 / `fp ext`）会追加进同一张表，`/help` 与补全实时反映。
运行中斜杠命令被禁用（防会话状态竞态），普通输入则排队穿插到本轮之后。

## 面板与显示

- **状态行**（顶部常驻）：`模型 · 会话 ID 前 8 位 · 空闲/运行中 · 提示`。
- **消息区**：`VerticalScroll` 消息流，流式输出按帧节流（约 20fps）；工具调用与返回是可折叠卡片，返回体超过 200 行会截断并标注总行数。
- **输入区**：多行输入框（最高 6 行）+ 发送按钮；占位符随忙闲切换，运行中提示「输入将排队注入（斜杠禁用）」。
- **补全浮层**：左候选窗 + 右用法说明，窄屏自动切成上下叠放，`Tab` 插入、`Esc` 关闭。
- **键位面板**（`Ctrl+K`）：选中动作按 `Enter` 进入捕获、按新键即改（立即生效并写回），`Delete`/`Backspace` 取消绑定，`R` 恢复当前项，`Ctrl+R` 全部恢复。
- **统计面板 / 外观面板**：会话统计（角色分布 + 长度曲线）与主题/配色/符号设置，出厂未绑定，用 `Ctrl+K` 分配。
- **问询弹窗**：Agent 向用户提问时弹出选项卡 + 自由输入框。
- **上下文对账**：命令或工具改写上下文后（`/sc`、`/back`、`/resume`、压缩等），消息区重建为新上下文的样子，并留一条「上下文已同步：视图重建（N → M 条消息）」——看到它即表示屏幕内容与 Agent 真正持有的上下文一致。

## 文档

- [CLI 入门](../guide/CLI入门.md)——命令行参数、快捷键、中断与上下文同步细节
- [命令参考](../guide/命令参考.md)——16 条命令的完整用法
- [会话管理](../guide/会话管理.md)——恢复、切换、清理历史会话
- [配置指南](../guide/配置指南.md)——`config.json` 全字段，含 `tui.theme` / `tui.keys` / `tui.symbols`
- [工具概览](../guide/工具概览.md)——Agent 侧工具与卡片渲染的对应关系

## 依赖

| 依赖 | 版本 | 用途 |
|------|------|------|
| `fp-core` | >= 0.2.0 | 引擎、会话、命令注册表与生命周期钩子 |
| `textual` | >= 8.0.0 | TUI 框架（渲染、键位绑定、屏幕与弹窗） |
| `rich` | >= 14.0.0 | Markdown / 富文本渲染 |
| `wcwidth` | >= 0.2.0 | CJK 宽字符度量（折行与对齐） |

Windows 上工具执行**强制使用 Git Bash**（`fp_core/platform_utils.py` 的 `find_bash`：`BASH_PATH` 配置 → PATH 中的 `bash.exe`（跳过 WSL 空壳）→ Git for Windows 常见安装位置）。找不到时给出安装指引，**不降级到 cmd.exe**。

## License

MIT，见仓库根 [LICENSE](../../LICENSE)。
