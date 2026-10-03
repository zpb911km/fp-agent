# 验收报告 — 下一代主动 agent 范式（#143）

日期：2026-09-30 凌晨 · 执行方式：**supervisor 自行实现**（agent_dispatch worker 于
02:24–03:46 时段 4 次启动即卡死，基建故障不可复现，遂转为亲自实现）

## 门禁

| 项 | 结果 |
|---|---|
| ruff check（全 packages） | ✅ All checks passed |
| ruff format（改动文件） | ✅ 6 files formatted |
| pyright（3 个核心改动文件） | ✅ 0 errors |
| pytest 全量 | ✅ 550 passed, 1 skipped（含新增 18 用例） |

## 两条真实会话链（端到端）

1. **后台启动→wait→消费**：`bash background=true` → job_id `544d69ee` →
   `wait_job` done → `read_file` 读到 `E2E-BG-RESULT-77`。
   会话落盘可见环顶注入：`user | 【系统事实】后台任务已完成…`，
   配对自查 3 tool_calls ↔ 3 tool msgs ✅
2. **ask→答→继续**：`ask_user` → tool result 只有收据
   `{"status":"answered","reply_file":…}` → `user | 【用户回答】选 A 方案…` →
   LLM 总结人类原话并结束。配对 1↔1 ✅

## 集成链（API 级，全部通过）

A: background→wait→drain 恰好一次（`【系统事实】`前缀） ·
B: `on_timeout=background` 让出→移交→后台收集成功 ·
C: `kill_job` 杀进程组+killed 注入 ·
D: 危险命令 headless 无 io → 回落拦截（不回归），`force=true` 放行仍在 ·
E: 正常 bash（成功/非零退出）不回归 ·
结构：drain 注入后 `repair_tool_ordering()` **fixes=0**（I2/I3 ✅）

## 不变量自查（设计 §8）

- **I1 中心性**：`kill_job`/退出清算（`_builtin_shutdown`→`shutdown_all`）独立于 LLM；
  ask 无 io 降级不挂死；`_sweep_stale` 死进程僵尸清扫（pid 探活防误杀并行实例）。
- **I2 权威注入**：人类话语只以 `【用户回答】` user 角色到达；系统事实只以
  `【系统事实】` user 角色到达；tool result 只有收据 → 配对 0 修复。
- **I3 shortcircuit 兼容**：注入均为 user 角色，degenerate 的删除对象
  （tool 消息 + AI 文本）不覆盖它们。
- docs 一致性测试：✅（README 17→18 已同步）

## 与设计文档的偏差（记录）

1. **worker 派发失败 → supervisor 自实现**：T1–T4 全部由本体完成，
   非设计预期的并行派发。worker 卡死根因未定位（等价复现 4 组合均正常，
   判定为 02:24–03:46 时段基建暂时故障；另发现 `~/.local/bin/env` 是
   会吞子进程输出的假 env，诊断过程一度被它污染）。
2. **危险确认门实现位置**：设计写「agent.py ON_BEFORE_TOOL」——该钩子不存在；
   实际落在 `core.py _execute_bash` 副作用检查内（更早、更内聚，
   headless 回落原拦截语义）。
3. **human_confirm 而非直接 ask_user**：确认门用 `human_confirm()`（返回
   True/False/None 三态），None→回落，保证 headless/worker 行为零回归。
4. **FRAMEWORK_JOB_TOKENS 挂点**：未实现（本轮无消费者；bash 移交直接走
   `start_job`）。斜杠命令 `jobs` / `kill` 未做——`list_jobs`/`kill_job` 工具
   已覆盖 LLM 侧，人类侧可读 `$TMPDIR/fp_jobs/`，状态条属前端工作后续做。
5. **设计文档同步**：`docs/dev/ASYNC_AGENT_DESIGN.md` §9 权限边界按实际写路径已履行；
   §0 仓库布局为修正后版本（初版路径全错，worker 空转的诱因之一）。

## 遗留（非本轮承诺）

- 注入消息的 ACP 下行推送（ACP 无 out-of-band 通道，现状等下轮上下文）
- WebUI 状态条 / `jobs` 斜杠命令
- ask 纪律的系统提示词强化段（现仅在工具 docstring）
- worker 派发基建的启动卡死（独立问题，已存反思）
