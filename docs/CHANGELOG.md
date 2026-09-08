# 更新日志

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/) 和 [Keep a Changelog](https://keepachangelog.com/) 规范。


## [0.1.13] — 2026-09-08

### Added

- **LLM 思考模式跨 provider 统一控制**（e8228a2）: `enable_thinking` 意图键按 provider 归一化（deepseek → `thinking:{type}`，qwen/其余保持原生布尔），`reasoning_effort` 透传至请求体顶层；`chat`/`chat_stream` 保留 `reasoning_content` 回传，带 tools 请求回传历史 CoT（DeepSeek v4 官方要求），不带 tools 时剥离省 token；`summarize` 强制关思考避免 API 报错。
- **shortcircuit 退化模式 degenerate**（d4fe2e5）: 删除块内工具调用/返回消息，将带 `tool_calls` 的 assistant 退化为纯文本记录；`protect_callsite` 保护进行中的调用点使 agent loop 不断开，连续纯文本 assistant 逆序合并消除非法结构；命令层 `-d`、工具层默认 mode 改为 degenerate。
- **任务状态机扩展 delivered / superseded**（5b20173）: 状态 3→5，`delivered`=交付待批挂起（LLM 停手等批准，`[task]` 显示 ⏸#N），`superseded`=用户推翻时作废旧交付，消除新旧完成记录叠加混淆；`task_clear` 清终态但不清 delivered。
- **bash 副作用检查**（7251712）: 执行前 BLOCK 高危命令（rm -rf 根/家/当前、pkill、磁盘操作、关机、fork 炸弹等），`force=true` 显式跳过；`timeout` 参数化（1~3600）；取消 WARN 层（事后提示在同步协议下无意义，调用记录本身透明可审计）。
- **插件对称注销 unregister_command / unregister_tool**（a88a5e3）: 插件禁用即清理其注入的命令与工具（文件命令受保护），shortcircuit / task_system 提供 `on_unregister` 成对清理。
- **system_prompt_append 多插件收集与重建重放**（5241cc0 / a41add3）: `apply_system_prompt_append` helper 归一化 str（旧覆盖式）与 `list[str]`（多插件收集），内置示范改 `setdefault().append()` 避免互相覆盖；webui 会话重建路径（新建/切换/恢复）消息组装后重放注入段，修复插件注入跨重建丢失。
- **IOChannel 前端身份标识**（b2e5949）: 新增 `frontend` 字段（terminal/webui/acp/rest），供扩展统一判断运行环境。
- **记忆系统自足性增强**（69bd923）: `memory_save` frontmatter 记录 `created`/`updated`（覆盖保留原创建时间）+ 可选 `hint` 参数存路径/命令等硬事实；`memory_read` 索引行内联 hint（免一次读取）、精确读取展示创建/更新时间、超 30 天未更新附 ⚠️ 过期提醒。
- **resume 会话列表倒序打印**（6db15fb）: 序号从大到小，`[1]`（最新）紧邻提示行，解决 600+ 会话时最新条目被顶出屏幕；`--main` 视图编号基于全量排序池，`[n]` 在两视图指向同一会话。

### Changed

- **LLM 供应点两级结构**（64a265e）: `LLM_PROVIDERS` 由一维 key 改为 `provider → {api_key, base_url, models → {model: 差异参数}}`，激活态收敛为 `ACTIVE_LLM="provider/model"` 唯一引用（取代顶层三键副本）；一供应商多模型不再需复制 key/base_url，同名模型天然消歧。模块加载时回填 `LLM_*` 常量使旧读者零改动，旧扁平格式内存兼容 + `/model` 侧一次性写回迁移，`TEMPERATURE`/`MAX_TOKENS` 回归全局默认语义。
- **`fp docs` 面向 agent 重设计**（ed93b7a）: 默认不再自动调用 xdg-open（曾阻塞工具调用）；空参/help 输出用法，`fp docs <相对路径>` 直接打印文档正文（realpath 越界防护 + difflib 相近文档候选），GUI 打开降级为显式 `--open`。
- **`fp ext` 参数错误友好化**（ed93b7a）: 空参显示完整帮助，错参只输出两行文档引导（去除 argparse 原始 error/usage 噪声）；`list` 各来源行尾打印资产目录绝对路径；子命令改由 main 前置路由（修复 `fp ext -h` 被顶层 argparse 劫持）。
- **shortcircuit 迁移至插件包**（7215600）: `/sc` 不再由 `commands/` 自动发现，改由插件 `ON_INIT` 注入注册，命令实现随插件包走，插件禁用时 `/sc` 一并消失；核心逻辑符号公共化，消除跨模块 cast hack。
- **全项目 pyright strict 类型债清零**（a8bff3c…606d186，18 个提交）: 2840 → 0 errors；5 包补 `py.typed` 标记；覆盖 core/prompts/tools/commands/plugins/cli/webui/acp/terminal 各层，`TypedDict` 精确 schema 去 `Any`。
- **工具调用显示优化**（9a5684d）: 参数改为每行一个，嵌套结构展开上限 400→4000。
- **bash 大输出预览结构化**（664e643）: 由「仅头部 200 字符」改为「头部 + 尾部 + stderr 摘要 + 行数统计」，避免漏掉通常在尾部/err 的关键信息。
- **工程卫生**（e860372）: 删除 `PROGRESS.md`（过程脚手架不应进 git 跟踪）。

### Fixed

- **task_update 永远匹配不到任务**（4b2173d）: `store.update` 用 `t.id == task_id` 严格比较，LLM 常把整数 `task_id` 序列化为字符串（`"2"`）导致恒 False，连带 `task_clear` 显示「没有已完成」；改为 `int()` 归一化 + 字符串兜底的双层宽容匹配。
- **任务管理语义与容错**（664e643）: `task_clear` 描述去除虚假的「更新 ID 序列」并返回被清明细；`task_update` 找不到任务给出补救提示；`store.load` JSON 损坏时告警 + 备份原文件（不静默清空），单条坏数据跳过保留好条目。
- **bash 在 dash 下的 Bad substitution**（6b689a6）: Linux 分支由 `create_subprocess_shell`（走 `/bin/sh`=dash）改为 `create_subprocess_exec(find_bash())`，根除 `${var:0:6}`/`[[ ]]`/heredoc 返工。
- **危险命令拦截器误拦与漏报**（6b689a6）: 命令位锚定 + 引号字面量剥离 + `rm` 危险目标收紧，引用性文本不再误拦，`sudo mkfs`/`shutdown` 等漏报兜住。
- **配置读取浅合并压制显式值**（e8228a2）: 兜底默认不再覆盖 model 级显式 `extra_body` 配置。
- **edit_file 缺参报错无引导**（6b689a6）: `ParamSpec.error_hint` 补「先 read_file 取哈希」提示。
- **测试跨用例状态污染**（6b689a6 / a23c752）: conftest autouse 还原 config 模块级 `LLM_*` 出厂常量，拦截 `set_active_llm_state()` 泄漏；`test_shortcircuit` 消除 7 处 `Any` 类型 warning。
- **`/model` 幽灵组件误报**（69bd923）: 实为 fp-ext 分发的用户级扩展命令而非仓库内置，`test_no_ghost_components` 增 `EXT_CMDS` 白名单消除 5 处误报。
- **memory_read_plugin.py 末尾重复残句**（69bd923）: 潜在语法错误清除。
- **配置优先级注释与实现不符**（507aa8f）: 修正为 config.json > 环境变量 > 默认值。
- **agent 提示词自举段新增 ext 资产说明**: `fp ext` 三来源（fetched/public/private，private 胜出）+ `list`/`info` 查询入口，agent 可自助定位扩展资产文件。
- **shortcircuit 迁移至插件包，命令层清空**: `/sc` 命令不再由 `commands/` 自动发现，改由 shortcircuit 插件在 `ON_INIT` 通过 `register_command("sc", command)` 注入——命令实现随插件包走（`plugins/shortcircuit/{core,command,plugin}.py`），`commands/shortcircuit.py` 删除；插件被禁用时 `/sc` 一并消失。核心逻辑符号公共化（`scan_components`/`degenerate`/`shortcircuit`/`parse_args`/`format_components_display`），插件层消除跨模块 cast hack；命令系统新增「插件注入命令」注册路径说明。

## [0.1.12] — 2026-08-04

### Added

- **`fp ext` 扩展分发系统（阶段一）**: 新增 `new`/`init`/`promote`/`demote`/`share`/`remove`/`fetch`/`install`/`info`/`list` 全命令体系，资产全生命周期管理；单仓库模型——promote 复制快照、share 全权管理 public 仓库（#42f9ad2, #a2eea4e）
- **promote/demote 移动模型 v3**: 分享=移动，全局单一版本（#b6d41f2）
- **三来源资产扫描**: option 命令支持 fetched/public/private 三种来源，自动去重（#af85c2c）
- **subagent 会话摘要与来源标记**: 子任务会话自动生成摘要并标记来源，支持 `/resume --main`（#d586b7f）
- **自举手册 `docs/self`**: 新增 agent 自举索引，提示词升级为"通用问题解决者 / 眼光长远 / 合作者姿态"（#bdf02dd）
- **文档同步门禁工具链**: 代码变更→文档跟进提醒（pre-commit 钩子 + skill 触发）+ 盲区审计 `--audit`（#86abc06, #bb1c965）
- **防漂移测试**: 文档一致性校验测试纳入 CI 门禁（#a37ab86, #3c2e00e）
- **fp ext 操作指南**: 新增完整使用文档（#adbe1db）

### Changed

- **fetch/install 两级抽象**: fetch 拉仓库扫描全部资产，install 提取资产本体（#693454f）
- **终端输入历史脱离资产库**: 历史记录迁移至 `terminal/`，废弃 MEMORY_DIR（#7a2837b）
- **默认 manifest 增加 license**: new/init 生成 GPL-3.0（copyleft 传染协议）声明（#9f88f55）
- **DOC_RULES 精细化**: 变更类型分层 + 模块精准映射 + 消除冗余（#12aacb5）
- **测试覆盖提升**: 修复过时断言，薄弱模块覆盖 41%→59%；pyright 60 错误清零（#2acd527, #5655d6f）
- **文档一致性修正**: 会话管理 / 命令参考 / 资产分发文档同步至最新代码行为（#67e4c25, #12f984c）

### Fixed

- **remove 精准删除 fetched 资产**: 按 registry 删除本体 + 识别孤儿资产，不误伤同名 public/private（#3b45d47, #e2fd0fc）
- **share 前置校验与幂等**: 资产须在 public + 分享仓库须有 fp.ext.json 清单 schema（#27834ef, #b469456）
- **资产查找支持 manifest 语义名**: 修复 list 不过滤 `__pycache__`（#ba3d7f3）
- **流式 usage 事件只发送一次**（#c5d88d0）
- **bash 工具防孤儿进程泄漏**（#0e49dbc）
- **ruff format 排除 markdown**: 统一本地与 CI 检查范围（#36c0e6b）

## [0.1.11] — 2026-07-31

### Added

- **`fp docs` 命令 + 离线文档随包分发**: 文档随包安装，支持 `fp docs` 直接查看，并接入 CI 发布流程（#4596aaa）
- **WebUI 快捷命令菜单**: 支持多级补全，命令面板数据源统一为后端动态接口（#333f194, #e686c6b）
- **WebUI 服务器日志改良**: 启动显示局域网地址，访问日志脱敏（#6bd1b32）
- **终端显示全面升级**: 工具调用覆盖思考、JSON 格式化 + 项目级截断、亮青 prompt、去双重横幅、输入块青色隔离线（#5fb8e8b, #e4fafd9, #2ee0844）

### Changed

- **rebrand: Five Pebbles/五块卵石 → FP**: 全仓库品牌更名，与 Rain World 角色切割（#b12fe98）
- **edit_file v3 — 哈希注册表 + 纯字符串替换**: 核心编辑工具升级，引入文件哈希注册表与陈旧检测，替换行号模式（#8cfe956）
- **core 四项改进**: 联合身份哈希 / ToolSpec 单一数据源 / 清理注册表 / 移除 bash 清扫（#5ec6dc7）
- **agent 提示词重构**: 精简为紧凑的角色/准则/风格结构，新增自举小节改用 `fp docs` 获取文档，弱化源码自修改；移除运行时状态信息的 system prompt 注入（#2414fe4, #63ee5d9, #6e5179d）
- **WebUI 重构**: 拆分 index.html + UI 全面升级 → 光环背景系统 + 混沌动画 + 登录页重构 → 精简装饰层恢复洁净基底（#e240ee5, #0fb4f3c, #5efffcb）

### Fixed

- **WebUI /reload 后 I/O 失效的根因修复**（#6901220）
- **安全加固**: 防御性过滤 system 消息污染、修复 WS 握手日志泄露 token 明文、expose 横幅清理（#ccbef33, #b6e1869, #cd76f9e）
- **会话摘要补齐**: 统一生成逻辑，修复 /resume 和 /reload 摘要缺失（#a095255）
- **Qwen API 流式响应 usage 字段丢失**（#53c066c）
- **终端**: 并行工具调用串行化输出、思考残留清理、_FP_SILENT 导入路径修复（#8b44298, #b0091f3）
- **`fp docs` 位置参数破坏子包参数透传**（#2c6610e）

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
| 0.1.13 | 2026-09-08 | LLM 供应点两级结构 + 思考模式统一控制 + shortcircuit 退化模式 + 任务状态机扩展 + bash 副作用检查 + 全项目类型债清零 |
| 0.1.12 | 2026-08-04 | fp ext 扩展分发系统 + 文档同步门禁 + 测试覆盖提升 + 多项修复 |
| 0.1.11 | 2026-07-31 | rebrand→FP + fp docs 离线文档 + edit_file v3 + WebUI 重构与安全修复 + 终端显示升级 |
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
