# display.py — Five Pebbles 终端显示模块

## 设计哲学

```
6 类输出 = 6 种终端信号

A. 操作反馈  →  绿色   →  用户操作后的直接回应
B. 行为提示  →  青色   →  系统主动给出的引导
C. 异常警示  →  红/黄  →  非预期路径（必须带解决指引）
D. LLM 流   →  灰/黄/默认 → 大模型的思考和输出
E. 系统日志  →  灰色   →  开发者调试用，默认隐藏
🎨 仪式感    →  青+粗体 →  品牌记忆点（启动/退出）
```

**核心原则**：用户扫一眼颜色就知道这条消息的性质，不需要读文字。

---

## 快速开始

```python
import display

# 一行替换 print
display.info("✅ 记忆已保存: my_note")
```

所有函数直接打印到 stdout，与 `print()` 行为一致。

---

## API 参考

### A. 操作反馈 — `info()` / `item()`

绿色，用于用户操作后的成功/信息反馈。

```python
display.info("✅ 记忆已保存: my_note")        # 绿色，关键信息
display.info("📂 新会话: s_250528_130000")    # 绿色
display.info("🗑️  已删除会话: session_abc")   # 绿色
display.info("🧹 历史已清空")                  # 绿色

display.item("   列表条目（默认色，调用方控制缩进）")
display.item("  • web_search (v1.0) [general]")
```

**何时用**：用户执行了 `/memory save`、`/session`、`/clear` 等命令后告诉用户结果。

---

### B. 行为提示 — `hint()`

青色，系统认为用户需要知道但用户没主动问的信息。

```python
display.hint("💡 输入 /help 查看命令")
display.hint("💡 使用 /resume list 查看可用会话")
display.hint("用法: /memory save <内容>")
```

**何时用**：启动后的首步引导、命令用法提示、操作建议。

---

### C. 异常警示 — `error()` / `warning()`

| 函数 | 颜色 | 场景 |
|------|------|------|
| `error(msg, fix)` | **亮红粗体** | 操作失败，必须带解决指引 |
| `warning(msg)` | 黄色 | 可恢复的异常，无需用户操作 |

```python
# 错误 — 必须带 fix 参数
display.error("❌ API 请求失败", fix="检查网络连接后重试")
# 输出:
# ❌ API 请求失败          ← 亮红粗体
#    → 检查网络连接后重试   ← 灰色缩进

# 警告 — 仅提示
display.warning("⚠️ 检测到重复响应模式，自动跳出循环")
```

**铁律**：`error()` 的调用方**必须**想清楚用户接下来能做什么，传给 `fix` 参数。只报错不给解决指引是不合格的。

---

### D. LLM 流 — `llm_thought()` / `llm_tool()` / `llm_output()`

| 函数 | 颜色 | 用途 |
|------|------|------|
| `llm_thought(msg)` | 暗淡斜体 | LLM 内部推理过程 |
| `llm_tool(msg)` | 黄色 | 工具调用参数/结果 |
| `llm_output(text)` | 默认 | 流式最终回复（`end=""`） |
| `llm_newline()` | — | 流式结束后的换行 |
| `llm_iteration(n)` | 灰色 | 多轮迭代统计 |

```python
display.llm_thought("用户的需求需要先查资料...")
display.llm_tool('  🛠️  web_search({"query": "Python history"})')
display.llm_tool('  📋  共 5 条结果')

# 流式输出（不换行）
for token in stream:
    display.llm_output(token)
display.llm_newline()

# 迭代统计
display.llm_iteration(3)
# 输出: 📊 本次交互共迭代 3 次
```

**注意**：`llm_output()` 使用 `print(text, end="", flush=True)`——它不换行，专为流式场景设计。

---

### E. 系统日志 — `debug()`

灰色，**仅在设置了 `DEBUG` 环境变量时可见**。

```python
# 启动时
display.debug("⚠️ 技能目录不存在：skills/")
display.debug("✅ 已加载技能：web_search")
display.debug("❌ 加载技能 file_manager 失败：...")
```

```bash
# 看不见 debug 输出
python3 agent.py

# 看得见
DEBUG=1 python3 agent.py
```

**何时用**：内部初始化过程、逐条的加载日志、不面向终端用户的诊断信息。

---

### 🎨 仪式感 — `startup()` / `shutdown_panel()` / `print_logo()`

青+粗体 + 框线，用于程序的启动和退出。

```python
# 启动横幅
display.startup("gpt-4")
# 输出: 🤖 Five Pebbels 已启动 (模型: gpt-4)  ← 青色粗体

# 结束面板
display.shutdown_panel(
    summary="用户询问 Python 历史",
    file="s_250528_130000.jsonl",
    model="gpt-4",
    msg_count=12,
    created="2026-05-28 13:00",
    duration="0:03:42",
)
# 输出:
# ╔════════════════════════════════════════════╗
# ║  📂  会话结束                                           ║
# ║────────────────────────────────────────────║
# ║  总结: 用户询问 Python 历史                             ║
# ...

# ASCII Logo
display.print_logo()
```

---

## 环境变量控制

| 变量 | 效果 |
|------|------|
| `NO_COLOR=1` | 禁用所有 ANSI 颜色（遵循 [no-color.org](https://no-color.org)） |
| `DEBUG=1` | 启用 `display.debug()` 输出 |
| stdout 非 TTY（管道） | 自动禁用颜色 |

```bash
# 管道模式自动无色
python3 agent.py | grep "error"

# 强制无色
NO_COLOR=1 python3 agent.py

# 调试模式
DEBUG=1 python3 agent.py
```

---

## 设计决策记录

### 为什么不用 emoji 参数而是让调用方传入？

每个消息的 emoji 是其语义的一部分，不是格式化的一部分。`display.info("✅ 保存成功")` 中的 `✅` 由调用方控制，保持了灵活性。如果 emoji 由 display 模块决定，调用方反而需要记忆「哪个函数对应哪个 emoji」。

### 为什么 `item()` 不做着色？

列表条目已经通过 `info()` 输出的标题区分了层级。条目本身用默认色，确保长列表阅读不疲劳。缩进由调用方控制，因为不同场景的缩进深度不同（`/memory list` 缩进 2 格，`/back` 缩进 4 格）。

### 为什么 `error()` 自带 `fix` 参数而不是让调用方拼字符串？

强制调用方思考「用户接下来能做什么」。如果调用方只写了 `display.error("出错了")` 没有传 `fix`，代码审查时就会被发现——这是设计上的人因工程。

### 为什么不直接用 Rich / Textual？

1. **零依赖** — display.py 只用 Python 标准库，不引入任何第三方包
2. **管道兼容** — Rich 在非 TTY 下会自动降级，但我们仍能保证 `display.info("...")` 在管道中输出纯文本
3. **最小化改动** — 将 80 处 `print()` 替换为 `display.xxx()` 是机械性的，不需要理解 Rich 的 API
4. **将来可升级** — display.py 的所有函数都是薄封装，未来切换到 Rich/Textual 只需改这一个文件

---

## 与原始 print 的对应关系

```python
# 原始 → 替换为
print(f"✅ 记忆已保存: {name}")              → display.info(f"✅ 记忆已保存: {name}")
print(f"📂 新会话：{id}")                    → display.info(f"📂 新会话：{id}")
print(f"   • {title}")                       → display.item(f"   • {title}")
print(f"💡 输入 /help")                      → display.hint(f"💡 输入 /help")
print(f"❌ API 错误: {e}")                   → display.error(f"❌ API 错误: {e}", fix="重试")
print(f"⚠️ 技能已存在")                       → display.warning(f"⚠️ 技能已存在")
print("\033[2m思考中...")                     → display.llm_thought("思考中...")
print(f"  🛠️  search()")                     → display.llm_tool(f"  🛠️  search()")
print(chunk, end="", flush=True)             → display.llm_output(chunk)
print(f"📊 迭代 {n} 次")                     → display.llm_iteration(n)
print(f"✅ 已加载技能：{title}")              → display.debug(f"✅ 已加载技能：{title}")  # DEBUG only
print(f"✅ 已加载插件：{name}")               → display.debug(f"✅ 已加载插件：{name}")   # DEBUG only
print(f"🤖 已启动 (模型: {m})")              → display.startup(m)
# shutdown 面板（多行框线）                   → display.shutdown_panel(...)
# print('''ASCII''')                         → display.print_logo()
```
