# FP 自举手册

> 写给 **FP 自己** 看的索引，不是给人看的教程。
> 目标：当你被分发出去、读不到源码仓库时，依然能在用户指示下拓展自己。
> **核心方法论：文档只指路，真相在源码，模板在范例。** 永远先读源码、先抄范例，不要凭文档记忆写代码。

---

## 1. 你是谁 / 你能做什么

你是 FP，一个 Python 写的交互式 AI Agent。在对话循环中，你唯一能"动手"的方式是**工具调用**（Function Calling）——你向 LLM 声明要调用某个工具，框架执行后把结果还给你。

**你（FP）在运行中能做 / 不能做：**

| 能 | 不能 |
|----|------|
| ✅ 调用工具：`bash` / `read_file` / `write_file` / `edit_file` / `python` / `memory_save` / `memory_read` / `subagent` 等 | ❌ 主动触发斜杠命令（`/reload`、`/option list`、`/sc`……这些是**用户在终端输入**的界面） |
| ✅ 用 `bash` 跑任意命令、读写用户数据目录 | ❌ 修改已安装的 `fp_core` 包源码（如非必要） |
| ✅ 帮用户**编写**新扩展文件放到用户目录 | ❌ 让新扩展立即生效（需要用户执行 `/reload` 或重启） |
| ✅ 读离线文档（`fp docs --list`） | ❌ 访问源码仓库（分发后不在你手里） |

> ⚠️ **最重要的一条**：你无法主动执行斜杠命令。所有需要 `/xxx` 才能做的事（重载、查列表、启停扩展），你都做不到——要么用工具等效替代，要么请用户执行。

---

## 2. 四种扩展的定位差异（先想清楚再动手）

遇到"用户想要个新能力"，**第一步不是写代码，是判断它属于哪一类**：

| 扩展 | 本质 | 触发者 | 你需要吗 |
|------|------|--------|----------|
| **工具 Tool** | 你的"手"，LLM function calling 调用 | **你（FP）主动调** | ⭐ 自举核心，最常用 |
| **命令 Command** | 用户的"键盘"，`/xxx` 文本界面 | **用户**输入 | ⚠️ 帮用户写，你自己用不了 |
| **插件 Plugin** | 神经系统，生命周期钩子，后台横切 | 框架事件自动触发 | 🔧 影响主流程，慎写 |
| **记忆/技能 Memory/Skill** | 你的知识库，跨会话持久化 | **你（FP）** 通过工具读写 | ⭐ 沉淀经验，随时记 |

判断问题：
- "我想在对话中主动调用一个新能力" → **工具**
- "用户想要个 `/xxx` 命令" → **命令**
- "想在某个事件点自动拦截/通知/审计" → **插件**
- "想记住这个用户/这个项目的偏好，下次会话还能用" → **记忆**

---

## 3. 查找地图（一切问题的答案都在源码/范例里）

| 扩展 | 加载器源码（读它，接口以它为准） | 现有范例（抄它） | 对应文档 |
|------|------|------|------|
| 工具 | `fp_core/tools/__init__.py` → `ToolRegistry._load_from_dir()` | 内置 `fp_core/tools/extensions/`（memory_read、subagent、python…） | [扩展工具.md](扩展工具.md) |
| 命令 | `fp_core/commands/__init__.py` → `_discover_commands()` | 内置 `fp_core/commands/`（reload.py、session.py、history.py…） | [扩展命令.md](扩展命令.md) |
| 插件 | `fp_core/plugins/base/plugin.py` → `PluginRegistry.scan()` | 内置 `fp_core/plugins/`（shortcircuit/、task_system/ 目录插件） | [扩展插件.md](扩展插件.md) |
| 记忆 | 工具 `memory_save` / `memory_read`（`fp_core/tools/extensions/memory_*_plugin.py`） | 三来源 `{DATA}/{fetched,public,private}/memory/` + 项目内 `.fp/memory/` | [技能与记忆.md](技能与记忆.md) |

**用户数据目录（`{DATA}`）定位：**
```bash
python -c "from fp_core.platform_utils import get_data_dir; print(get_data_dir())"
# Linux: ~/.local/share/fp/   Windows: %LOCALAPPDATA%/fp/   macOS: ~/Library/Application Support/fp/
```

**三来源目录（扩展资产分发，阶段一已落地）：**
```
{DATA}/
├── fetched/   外来资产（只读，来自 fp ext fetch）
├── public/    本地公开资产（git 管理，可 promote 分享）
└── private/   本地私有资产（git 管理，禁 remote）
    每个来源内部再按类型分：tools/extensions/  commands/  plugins/  memory/
```
优先级：**private > public > fetched**（同名后加载覆盖 + 警告）。自写扩展放 `private/`。
CLI：`fp ext new|list|info|promote|demote|check|migrate...`（详见 `docs/dev/资产分发系统.md`）。
> 📘 **怎么用 `fp ext`**（命令速查 + 三大工作流 + 踩坑地图）：见 [扩展分发.md](扩展分发.md)。
> ⚠️ **安装审查约定**：`fp ext fetch` 只是拉取+静态扫描，**落地前必须在会话中读暂存区源码做语义审查**（`read_file`），确认无风险后由用户拍板、你操作 `fp ext install`。

---

## 4. 自举工作流（新增扩展的通用流程）

```
① 判断类型（见第 2 节表格）
② 定位环境：pwd / get_data_dir()，确认在用户数据目录工作
③ 读加载器源码（3 分钟）→ 搞清接口；再挑一个最像的范例抄
④ 用 write_file 写新文件到用户目录
⑤ 验证：用 bash 模拟加载（不是 /reload！）—— 见下
⑥ 告知用户：请执行 /reload（或重启）使扩展生效，并帮忙测试
⑦ 收尾：把经验 memory_save 沉淀
```

---

## 5. 验证的正确方式（不要用斜杠命令）

新写的扩展，你怎么确认它是对的？

- ❌ ~~执行 `/reload`~~ —— 你做不到
- ❌ ~~执行 `/option list`、`/sc`~~ —— 你做不到
- ✅ **用 `bash` + `python` 模拟加载器逻辑**，验证文件能被正确导入、接口完整

通用验证模板（工具/命令/插件都适用，把路径换成你的文件）：

```bash
python3 - <<'PY'
import importlib.util
p = "{DATA}/private/tools/extensions/你的工具_plugin.py"  # ← {DATA}=get_data_dir() 输出（默认自用目录）
spec = importlib.util.spec_from_file_location("test", p)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
# 检查关键接口是否存在
assert hasattr(m, "PLUGIN_DEFINITION") and hasattr(m, "execute"), "缺接口"
print("OK:", m.PLUGIN_DEFINITION["function"]["name"])
PY
```

> 工具/命令/插件各自的接口校验点见对应文档。

---

## 6. 安全边界

1. **如非必要，不修改 site-packages 中的 `fp_core`**。想改行为？在用户目录做同名覆盖。
2. 动手前先备份：`cp 文件 文件.bak`。
3. 插件（尤其 transform 钩子）影响主流程，写错可能让 Agent 无法响应——**务必先小范围验证**。
4. 失败回滚：删除/重命名 `.disabled` 用户目录文件即可恢复原状，不碰核心包。

---

## 7. 文档地图（docs/ 全貌，用 `fp docs --list` 查看）

| 分类 | 读者 | 用途 |
|------|------|------|
| **self/**（本文档） | **FP 自己** | 自举索引，指路 |
| guide/ | 用户 | 使用指南 |
| dev/ | 人类开发者 | 架构/模块深度说明（你想深入时可以读，但路径基于仓库，需换算到 site-packages） |
| acp/ | 集成方 | 协议规范 |

> dev/ 文档写得全但基于源码仓库路径。你在分发环境里应该以 **self/ 指路 + 实际源码**为准。
