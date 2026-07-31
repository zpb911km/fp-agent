# 更新日志

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/) 和 [Keep a Changelog](https://keepachangelog.com/) 规范。

> **📌 版本号修正说明**
>
> 此前版本号存在虚高（2.0.0/1.1.0/1.0.0 与实际功能成熟度不匹配）。
> 现已统一修正为 `0.1.0`，与 `pyproject.toml` 保持一致。
> 以下版本号保留原始记录以供追溯，实际发布包版本均为 `0.1.0`。


## [0.1.10] — 2026-07-27

### Added

- **端到端流式输出管道**: 打通 LLM 调用层流式数据管道，配合 `rich.live.Live` 实现流式 Markdown 实时渲染，思考过程流式清除显示，终端体验全面升级（#7662567, #1055850, #47e267c）
- **`fp --update` 自动更新**: 新增 CLI 入口参数，支持一键自动更新到最新版本（#e78dfaa）
- **edit_file v2 — 行号替换模式**: 核心工具升级，新增行号定位替换、文件哈希陈旧检测、精简 22% 代码量（#d1d990b）

### Changed

- **权责重划 — 终端代码迁入 `fp-terminal`**: 将终端相关代码从 `fp-core` 迁移至 `fp-terminal`，职责边界更清晰（#750e25d）
- **启动 LOGO 替换为动态扫描光带**: 告别静态文字，改用动态光带动画作为启动面板，视觉冲击力更强（#25e3c8e）

### Fixed

- **流式降级状态正确显示**: 流式模式不显示 spinner，非流式降级时正确切换回 spinner 提示（#2c177bd）
- **`think()` 配色使用 rich Style 替代 ANSI 转义**: 修复在 rich 布局中 ANSI 转义序列配色失效的问题，正确渲染主题配色（#c2c71d6）
- **修复命名碰撞导致的类型错误**: 消除因模块间命名冲突引发的运行时类型错误（#6e87829）



## [0.1.9] — 2026-07-14

### Added

- **Token 用量追踪系统**: LLMService 返回 `LLMResult(message, usage)` 统一携带 token 用量；新增 `token_tracker` 模块管理调用统计；`shutdown_panel` 展示 `↑prompt ↓completion` 及缓存命中率；新增 `/token` 命令实时查看当前会话用量（#12666b6）
- **多智能体基础设施 — 角色系统**: Agent 新增 `role` 参数，支持按角色覆盖 system prompt、切换 LLM 模型/温度、工具白名单过滤。角色与 agent 实例分离，为 AgentPool 铺路（#55d0109）
- **动态命令注册**: 新增 `register_command()` 函数，供生命周期插件动态注册命令，支持别名与冲突检测（#55d0109）
- **热重载引擎 `AgentReloader` + `/reload` 命令**: 支持运行中热重载扩展代码，无需重启进程（#5d6de2c）
- **`/option` 命令**: 统一管理三种扩展机制（插件/命令/工具），支持编号操作和目录型插件的 enable/disable（#ebbfa98, #6ab0186, #49e78c9）
- **工具并行执行**: Agent 工具调用阶段支持并行执行多个工具，配合 `ON_TOOL_ERROR` 钩子增强容错（#c843ac3）
- **shortcircuit 工具 + 默认 crop 模式**: 新增 shortcircuit 独立工具，默认 crop 模式减少 token 消耗（#d1d5d7e）
- **CLI `--version` 参数**: 显示 `fp` 版本号后退出（#8f9a7d0）

### Changed

- **Agent 上帝类剥离**: 创建 State 大通道接口，将状态管理职责从 Agent 解耦，修复 7 项耦合问题（#ef466f5, #7710bec）
- **核心工具迁移至 `tools/` 包**: `compact`/`back`/`shortcircuit` 等策略逻辑从 core 迁移到命令层，完成工具从 core 到 tools 的完整迁移（#9de5c6e, #a9e98de）
- **`tools/plugins/` → `tools/extensions/` 语义重命名**: 消除"插件"与"扩展"的概念混淆，使类型层次更加正交（#79a9cd2）
- **旧 task 工具重构为生命周期插件**: 任务系统从独立工具转型为插件化的生命周期钩子，同步更新文档（#b3d06a3, #e3e4970）
- **移除旧 `model` 命令，新增 `/new` 命令**: 统一会话创建入口，修复 webui 会话切换（#53af2e1）
- **移除 IOChannel 废弃方法**: 清理 `hint`/`item` 等过期接口（#1ead158）
- **清理弃置类死代码**: 移除不再使用的类定义（#3b751ac）
- **清理过时的项目本地记忆文件**: fp-memory 遗留文件统一清理（#2d4c81c）

### Fixed

- **`/reload` 热重载后 Agent 引用未交换（严重）**: 热重载后请求仍走向已 shutdown 的旧 Agent，修复为正确交换引用（#bbce65a）
- **热重载后目录插件子模块残留导致钩子丢失**: 修复模块热替换时子模块对象未被清除的问题（#b2deff7）
- **清理 `sys.argv` 防止子包 parser 被顶层参数阻塞**: `fp` 入口未清理 `sys.argv`，导致 argparse 被子包调用时误读顶层参数（#339024c）
- **全项目类型错误修复**: 消除 mypy/pyright 零告警，覆盖 26+ 处类型问题（#a5f6e4a, #3d8d939）
- **多轮 LLM 调用时 `CLIIO._streamer` 为 None 导致崩溃**: 修复流式输出在第二次 LLM 调用时的空指针崩溃（#4657599）
- **显示层异常误报为 API/LLM 错误**: 修复 display 模块内异常被错误归类的问题 + 3 项残留 bug（#9d09513）

## [0.1.8] — 2026-06-26

### Added

- **项目原则文档**: 新增 `docs/项目原则.md`，记录关键架构决策与设计约束（如"先改 AST 后落地"、"零全局状态"、"外置扩展优先"），作为后续开发与评审的契约依据
- **`.fp/` 纳入版本管理**: 将 Agent 本地记忆目录加入 `.gitignore`，允许每个项目仓库独立维护本地 `.fp/` 而不被误提交

### Changed

- **记忆系统重构（双根树 + 三层结构）**: `memory_save`/`memory_read` 全新接口，按 root（`~/` 全局 / `./` 本地）、category、name 三层寻址；`prompt_builder` 索引从平铺改为分类分组显示，行数从 79+ 压缩至约 25 行，语义明确；记忆搜索支持跨分类全文检索；19 个 skill 文件统一整理（#74b9aaa）
- **shortcircuit 提炼提示词重写**: 从"重述对话"改为"第一人称操作日志"风格，新增 8 条结构化规则（保留关键产出、信息密度优先、过程精炼结果展开等），`max_tokens` 从 10000 调整为 8192，压缩质量显著提升
- **命令输出统一为 Markdown 单一通路**: 所有 15 个命令的 `execute()` 统一返回 Markdown 格式字符串，terminal 用 rich 渲染、webui/acp 各自消费，消除前端重复显示与双路输出的不一致问题（#71927ab）
- **工具钩子升级为 transform 型**: 工具执行阶段的 4 个钩子（`before_call`/`after_call`/`before_add_msg`/`after_add_msg`）升级为 transform 模式，支持拦截、修改返回值和人工审核流程
- **resume 命令重构**: 支持纯数字序号 → session ID 映射、Markdown 特殊字符转义、无参数时默认执行 list，提升交互流畅度
- **提示词与压缩策略优化**: 补充上下文管理提示词，优化压缩时机与策略，降低 token 消耗

### Fixed

- **shortcircuit 块重建乱序（严重）**: `#/sc N` 只对最后一个块生效——根源是 `shortcircuit()` Step 3 重建时递增遍历 user_idx，但最新块选中后 i 已越过老块的 user_idx，导致较早的块被跳过未压缩。修复：Step 2 处理前对 indices 按 user_idx 升序排序
- **WebSocket 协议空字段导致回复不渲染**: `llm_end` 事件收到 content 但未传递给前端；`done` 事件缺少 `final_content` 渲染入口，回复全部不显示；同步清理了永不触发的 `response` 分支死代码。附：修复 ACP 工具输出双倍行距问题
- **`process()` 缺失 io 上下文（安全隐蔽）**: CLI 调用 `process(msg)` 不传 io 参数时 `_current_io` 为 None，审计插件 `get_current_io()` 返回 None 后静默放行，拦截完全失效。修复：`_current_io.set(io or self._default_io)` 确保各通道统一路径

## [0.1.7] — 2026-06-20

### Fixed

- **空 session 文件爆炸**: `save_context()` 在 msg_count=0 时跳过文件创建，修复 Agent 初始化后未 process 就 shutdown 产生空 JSONL 文件的问题（153/385 个会话为空，占比 ~40%）

## [0.1.6] — 2026-06-20

### Added

- **PyPI 版本自动检测**: 每次运行 `fp` 时后台线程查询 PyPI JSON API，检测 fp-agent/fp-core/fp-terminal/fp-webui/fp-acp 五个包是否有新版本，发现即提示
- **`/sc` 命令**: 新增短路命令 `/sc`（Short Circuit），一键压缩已完成的对话连通块（连续 closed 消息），提升长会话可读性
- **编号前缀统一**: `/sc` 输出中任务编号使用 `#ID` 前缀，与 display 格式保持一致

### Changed

- **彻底铲除旧技能系统**: 删除 `skills/` 目录全部残留文件及 9 篇文档中对该系统的引用，完成向插件体系的迁移
- **`/sc` 命令主名**: 命令名改为简短 `sc`，移除冗余 aliases
- **`/sc` 范围压缩**: `@M-@N` 格式的连续范围合并为单一压缩符号 `~`

### Fixed

- **`/sc` refiner 数据重复**: 修复 refiner 过程中多次读取导致的数据重复问题
- **`read_file` 截断**: 添加默认截断行为（200 行），防止超大文件一次返回撑爆上下文
- **bash 混合输出策略**: 小输出直接返回，大输出（≥3K）自动保存文件+预览，避免终端缓冲区溢出

## [0.1.5] — 2026-06-17

### Added

- **自举内部文档**: 新增 `fp_core/_self_docs.py` 模块，包含 12 个章节的结构化架构知识（系统概览/生命周期/插件/命令/工具/技能/会话/自修改/配置/LLM抽象/主循环流程图/GitHub资源指引），`pip install fp-core` 后即用，提供 `find_tool()` / `find_command()` / `find_hook()` / `get_summary()` 等辅助函数
- **插件管理技能**: 新增 `plugin_management` 技能，通过重命名文件实现插件的启用/禁用

### Changed

- **docs**: 重命名 `plugins.md` → `文件命名约定.md`，新增四类扩展（插件/命令/工具/技能）对比表
- **docs(plugins)**: 插件输出示例统一使用 `display.info()` 替代 `print()`
- **skills(subagent)**: 精简技能描述，去除冗余内容
- **history**: `print()` 输出改为 `display.info()`/`display.item()`，静默模式下不泄漏输出
- **history**: 命令执行结果返回给 IDE 调用方而非仅打印到终端

### Fixed

- **core**: `rebuild_context()` 改用 `reset()` 替代 `set_system_prompt()`，修复上下文重建时历史残留导致的 prompt 错乱
- **core**: 核弹退出 (`exit!`) 不再残留会话文件
- **subagent**: 真正的静默模式 — 抑制 spinner / LLM 流等 UI 输出，工具结果纯文本化
- **acp**: 改用 `rawInput`/`rawOutput` 符合 ACP v1 规范
- **acp**: 修复并发 prompt 防护、session_id 快照、session-ping 等竞态问题
- **acp**: 修复取消失效、崩溃恢复、毁灭命令过滤等边缘情况

## [0.1.4] — 2026-06-16

### Changed

- **docs**: 快速开始文档初始化方式从"自动生成"改为显式 `fp --init` 命令，CLI 参数列表补全 `--init` 条目

### Fixed

- **config**: `init_config()` 写入 `config.json` 前递归创建父目录，修复配置目录不存在时 `FileNotFoundError` 崩溃（"爷目录不存在"问题）
- **platform_utils**: `find_bash()` 加模块级缓存避免重复扫描 PATH；WSL 空壳 bash 启动器检测（文件大小 + FileDescription）；`BASH_PATH` 配置优先级提升（`config.json` > PATH）；`ansi_supported()` 替换 `colorama` 为 Win32 `GetConsoleMode` API
- **tools/core**: cmd.exe 回退降级时先 `chcp 65001` 切换 UTF-8 代码页；UTF-8 解码出现 `\ufffd`（替换字符）时以 locale 编码重试
- **tools/python_plugin**: 子进程设置 `PYTHONIOENCODING=utf-8` 环境变量，防止 GBK/cp936 locale 下 Unicode 字符解码崩溃

## [0.1.3] — 2026-06-15

### Added

- **跨平台兼容框架**: 新增 `fp_core/platform_utils.py` 模块，统一检测平台类型、定位 Git Bash、适配路径格式
- **Windows 自动路由**: `tools/core.py` 在 Windows 上优先使用 Git Bash 执行命令，无 Git Bash 降级为 `cmd.exe` 并通知 LLM

### Changed

- **路径系统**: 替换全部 6 处硬编码 XDG 路径引用，使用 `platform_utils` 动态适配 Linux/macOS/Windows
- **文档**: 更新 6 份文档，要求 Windows 用户安装 Git for Windows

### Fixed

- **webui**: 修复 `os.chmod` 在 Windows 上不支持的兼容性问题

## [0.1.2] — 2026-06-15

### Fixed

- **版本格式**: `bump_docs.py` 正则改为 `v?[\d.]+`，兼容无 `v` 前缀的版本号
- **文档同步**: `bump_docs.py` 替换后不再残留 `v` 前缀，与 setuptools-scm 保持一致

## [2.0.0] — 2026-06-07

### 🎉 重大重构：生命周期驱动的 Agent 框架

#### 新增

- **生命周期系统**：重构项目为基于 `LifecycleManager` 的插件化架构，23 个生命周期钩子覆盖全流程
  - 初始化阶段：`ON_INIT`, `ON_CONFIG_LOADED`
  - 消息处理：`ON_MESSAGE_RECEIVED`, `ON_MESSAGE_PARSE`, `ON_MESSAGE_FILTER`
  - 执行阶段：`ON_BEFORE_THINK`, `ON_THINK`, `ON_AFTER_THINK`
  - LLM 交互：`ON_BEFORE_LLM_CALL`, `ON_LLM_CALL`, `ON_AFTER_LLM_CALL`
  - 响应阶段：`ON_BEFORE_RESPONSE`, `ON_RESPONSE`, `ON_AFTER_RESPONSE`
  - 工具执行：`ON_TOOL_SELECT`, `ON_TOOL_CALL`, `ON_TOOL_RESULT`, `ON_TOOL_ERROR`
  - 资源管理：`ON_SHUTDOWN`, `ON_CLEANUP`
- **插件自动扫描**："文件即开关"机制，通过文件系统命名约定控制插件启停
  - `name.py` → 启用，`name.py.disabled` → 停用，`_name.py` → 跳过
- **通知插件**：监听 lifecycle 事件，支持桌面通知和声音提醒
- **非流式调用 Spinner 动画**：LLM 响应等待期间显示旋转动画
- **`exit!` 核弹级退出命令**：强制退出而不保存会话

#### 修复

- **Ctrl+C 中断修复**：`signal.signal()` + `_stream_chat` CancelledError 优雅捕获
- **跨平台加固**：signal handler 双保险机制（`all_tasks` + 全局标志）
- **Pylance 类型错误清零**：25 errors → 0，全项目类型安全
- **人称视角混淆**：新增【自我认知规则】，修复第三人称/第二人称混淆
- **`_stream_chat` 非流式化**：修复未绑定变量警告

#### 重构

- **LLM 客户端重构**：外置 `llm/` 模块移至 `fp_core/core/llm_client.py`
- **`_stream_chat` 逻辑重构**：移除流式中间态，统一为整体返回
- **工具系统增强**：改进异常处理和字符串替换验证

### 依赖变更

- `httpx` → 核心依赖，替换 openai SDK
- 新增：`rich`, `wcwidth`（终端显示）
- 新增：`ddgs`（DuckDuckGo 搜索）
- 新增：`prompt_toolkit`（交互式 CLI）
- 新增：`PyYAML`（技能配置解析）

---

## [1.1.0] — 2026-06-01

### 新增

- **Subagent 派遣系统**：支持创建独立子 agent 执行离线任务，含输出契约（静默/调试/格式控制）
- **自我认知规则**：修复 agent 人称视角混淆，所有自言自语句强制使用第一人称
- **工具系统增强**：
  - `web_search` — DuckDuckGo 搜索
  - `web_fetch` — 网页内容抓取
  - 异常处理和字符串替换验证加强

### 修复

- `memory_save_plugin` 参数获取问题
- 自动驱动功能导致的问题（已注释）

### 变更

- 任务系统完全解耦为自包含插件
- 颜色/样式/截断配置移至 `~/.config/fp/config.json`

## [1.0.0] — 2026-05-28

### 重大变更：自实现 LLM 客户端

#### 新增

- **自实现 HTTP 客户端** (`fp_core/core/llm_client.py`)：替换 `openai` SDK，减少外部依赖
- **`display.py` 显示模块**：6 类输出函数 + Spinner + LLMStreamer
- **`<think>` 标签提取**：自动从 LLM 响应中提取 reasoning_content
- **配置管理系统**：三级优先级（JSON > 环境变量 > 默认值）
- **Rich 终端渲染**：替换自定义 Markdown 渲染，支持语法高亮和彩色输出
- **压缩功能**：对话历史智能压缩，控制 token 占用

#### 变更

- 全部 `print()` 替换为分类输出函数
- 配置从硬编码迁移至 `~/.config/fp/config.json` 文件管理

#### 修复

- 25 个 Pylance 类型错误全部修复
- 上下文持久化和工具调用处理重构

## [0.1.0] — 2026-05-27

### ✨ 初次发布

- FP 智能体基础框架
- 基于 openai SDK 的 LLM 客户端
- 基础对话历史管理
- 记忆管理模块 (`memory.py`)
- 技能系统 (`fp_core/skills/`)
- 工具调用系统 (`fp_core/tools/`)
- 命令系统 (`fp_core/commands/`)
- 对话修复与智能压缩

---

## 发布历史

| 版本 | 日期 | 摘要 |
|------|------|------|
| 0.1.10 | 2026-07-27 | 端到端流式输出 + fp --update + edit_file v2 + 终端权责重划 |
| 0.1.9 | 2026-07-14 | Token 用量追踪 + 角色系统 + 热重载引擎 + 工具并行 |
| 0.1.8 | 2026-06-26 | 记忆系统重构 + shortcircuit 提示词重写 + 命令输出统一 |
| 0.1.7 | 2026-06-20 | 修复空 session 文件爆炸 |
| 0.1.6 | 2026-06-20 | PyPI 版本检测 + /sc 命令 + 旧技能系统清理 |
| 0.1.5 | 2026-06-17 | 自举内部文档 + 插件管理技能 + 多项修复 |
| 0.1.4 | 2026-06-16 | 待补充 |
| 0.1.3 | 2026-06-15 | 待补充 |
| 0.1.2 | 2026-06-15 | 待补充 |
| 2.0.0 | 2026-06-07 | 生命周期驱动的 Agent 框架重构 |
| 1.1.0 | 2026-06-01 | Subagent 派遣系统 |
| 1.0.0 | 2026-05-28 | 自实现 LLM 客户端 + 显示模块 |
| 0.1.0 | 2026-05-27 | 初始版本 |

> 注：以上版本号为迭代记录标识，实际发布包版本统一为 `0.1.0`（详见顶部说明）。
