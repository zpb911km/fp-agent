# 下一代主动 Agent 范式：设计文档 v1（唯一权威依据）

> 任务 ▶#143。目标：把用户输入变成 LLM 的主动行为（pull），并让工具后台执行、
> 完成后注入、LLM 用 wait_job 精确拉取。核心问题：pull 缺唤醒会退化为 sleep 轮询 ——
> 本轮合流方案的解法是**轮次内闭环**（ask_user / wait_job 在工具内阻塞 await，
> 不需要轮次结束、不需要 auto-wake）。

## 0. 范围（DONE 判据）

> 仓库布局：monorepo，核心在 `packages/fp-core/src/fp_core/`（工具在 `tools/`，
> 核心循环在 `core/agent.py`），CLI 在 `packages/fp-terminal/src/fp_cli/`，
> 测试在 `packages/fp-core/tests/`（pytest，pytest-asyncio）。工具注册看
> `tools/__init__.py` 与 `tools/core.py` 现有工具类结构。

1. `packages/fp-core/src/fp_core/tools/background.py`：`ask_user` + `jobs`（框架服务）+ `wait_job`/`kill_job`/`list_jobs`，一个文件自洽，只依赖 `fp_core.core.io` 的 ask 原语。
2. `packages/fp-core/src/fp_core/core/agent.py` 接入 jobs 惰性 drain（环顶）+ shell 危险确认门 + await 令牌完成的最小挂点。
3. 三个不变量（硬验收，见 §8）+ 权威注入原则。
4. 三测试文件全绿 + pyright 0 error + ruff clean。
5. 更新 docs：`fp docs --list` 找到工具列表文档补 4 个新工具说明。

**明确不做**（本轮）：bash 的 `background=true` 参数改造、two-phase 确认协议（CP2/CP4 那套）、ACP 独立 ask 通道、prompt_builder 的 ask 纪律段、状态条/前端侧边栏。

## 1. 权威注入原则（最重要，违反=返工）

- **用户输入 = user 角色消息**。永不放进 tool result。
- tool result 只放回执/元信息（收据），人类话语以带标记的 user 消息到达。
- **系统事实注入 = 框架写的注入**，不消耗 LLM 输出、不会被当数据。
- LLM 主动发起（ask_user/wait_job）+ 框架被动通知（完成 drain）分工。

## 2. ask_user（pull 工具）

```python
# signature
async def execute(params) -> str   # 缺 io → 返回错误串（不 raise）
# params: {"prompt": str 必填, "suggest": str 可选（推荐默认值，防滥问纪律）}
```

流程：
1. 调用 `await io.ask(prompt)`（四前端原语已备，轮次自然挂起，无状态机）。
2. 回答到达 → **两条**写入对话：
   - `{"role":"user","content":"【用户回答】<原文>"}`（由工具调用 agent 的 append 钩子或返回结构约定；若协议修复 accept_user_midturn=True 则免）
   - 工具返回 `{"status":"answered","reply_file":"/path/ask.json"}`（分离：完整原文落临时文件，tool result 只给收据）
3. **防滥问纪律写进工具 docstring**：只问阻塞级决策；必须给推荐默认值；话题级问题仍走结束轮次。
4. 缺 io（worker/无头）：返回 `{"status":"unavailable","error":"no_io"}`，不 raise。
5. 并发：**单 flight 锁**（module-level asyncio.Lock），第二个 ask_user 返回 `{"status":"busy"}`。主 agent 独占由结构保证（worker 无 io），docstring 写明。

## 3. jobs 框架服务（background.py 内）

```python
JOB_DIR = Path(tempfile.gettempdir()) / "fp_jobs"
# 每 job 一个文件: {job_id}.json = {id, label, status: running|done|failed|killed,
#   started_at, finished_at, result_path, error}
@dataclass Job: ...  # in-memory
```

- `start_job(label, coro) -> job_id`：内部创建 asyncio 任务，**完成回调只写文件+入队**（不阻塞）。
- **任务来源**：本轮用"**await 令牌完成**"最小挂点：agent 提供 `FRAMEWORK_JOB_TOKENS: dict[token] -> Future`（给未来 shell background 移交用）。jobs/wait_job 服务 ready 的 token，测试用 mock future 注入。
- drain：agent 环顶 `drain_ready()` → 消息形如
  `{"role":"system"...}` 或带前缀 user 消息：`【系统事实】后台任务 <id> 已完成，结果见 <path>（状态：done）`。
  **drain 只入队，注入在环顶串行执行**（无锁竞态）。
- **僵尸清理**：session 结束/进程退出时，running job → 标 killed（本轮不做跨进程 reattach）。
- 完成注入内容是**框架写的系统事实 → 带标记注入**，不落 tool result。

## 4. shell 危险确认门（ask_user 的杀手应用）

`agent.py` 的 `ON_BEFORE_TOOL`（返回 bool 拦截）：工具名 bash 且 shell 含危险模式
（清单在 background.py 导出 `DANGEROUS_SHELL_PATTERNS`：`rm -rf` / `mkfs` / `dd if=` /
`> /dev/sd` / `shutdown` / `chmod -R 777 /` 等）且未带 `confirm=true` 参数 →
`await ask_user(f"将执行危险命令：{...}，确认？", suggest="n")`；回答 y/yes →
改写 params 加 `confirm=true` 放行；否则返回"用户拒绝执行"给 LLM。
**这取代"LLM 自行授权 force=true"的自审批。**

## 5. wait_job / kill_job / list_jobs

- `wait_job({"job_id": str, "timeout": float 可选})` → 阻塞 await Future（**pull 正确形态**：
  精确在完成一刻返回，不 sleep 轮询）。返回 done/result_file；timeout →
  `{"status":"still_running","elapsed":...}`；killed → killed。
- `kill_job({"job_id"})` → 置 killed + 终止 future。
- `list_jobs({})` → 全部 job 摘要（**可见性独立于 LLM**：用户可查同一张表）。

## 6. 框架挂点（agent.py，最小侵入）

1. **环顶 drain**：`_process_inner` while 顶部、`_call_llm` 前：
   `drain_ready()` 的系统事实消息 append 进 `messages`（走 `ON_CTX_APPEND`）。
2. **jobs 状态独立落盘**：JOB_DIR 文件即持久化（不混进 context.save）。
3. **退出清算**：session 结束/退出钩子里 `background.shutdown_all()`。
4. `FRAMEWORK_JOB_TOKENS: dict[str, Future]` 供未来 shell background 移交注册。

## 7. 测试（三文件，TDD 先行）

- `packages/fp-core/tests/test_ask_user.py`：缺 io；正常回答包装为 user 消息 + 回执 file；单 flight busy；suggest 默认值。
- `packages/fp-core/tests/test_jobs.py`：start_job 完成→文件 done；drain 恰好一次；killed 路径；僵尸清理。
- `packages/fp-core/tests/test_wait_kill.py`：wait 完成/超时/已 killed；kill_job；list_jobs。
- 参照 `packages/fp-core/tests/test_shortcircuit.py` 的模式（pytest.mark.asyncio）。

## 8. 硬验收（不变量）

- **I1 中心性**：`is_processing` 期间用户消息仍可 drain（现有路径不回归）；Ctrl+C 无条件杀任务（jobs 表 killed 清算）；kill_job/UI 可见性独立于 LLM。
- **I2 权威注入**：用户话语只以 user 角色到达（tool result 只有收据）；系统事实只以带标记的注入到达；**协议配对**：每个 tool_call_id 恰一条 tool 消息（repair_tool_ordering 后 0 错误）。
- **I3 shortcircuit 兼容**：degenerate 之后上下文仍合法（tool_call 配对完好、user 消息不被删）。
- pyright（严格配置）0 error；ruff check+format 通过。

## 9. 权限边界（每个任务允许的写路径）

- T1 测试：仅 `packages/fp-core/tests/test_{ask_user,jobs,wait_kill}.py`
- T2 工具：仅 `packages/fp-core/src/fp_core/tools/background.py`（+ 注册表导出，若需要）
- T3 挂点：仅 `packages/fp-core/src/fp_core/core/agent.py`（环顶 drain、确认门、框架挂点）
- T4 集成：`background.py` 缺口补齐 + docs 更新（`fp docs --list` 查到的工具文档）
- 禁改：shortcircuit 插件、prompt_builder、io 四前端（若需 io 扩展，报回 supervisor）。
