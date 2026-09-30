# ACP 协议

> ACP（Agent Communication Protocol）是本项目的 JSON-RPC 2.0 远程调用协议，
> 由 `fp-acp` 子包实现。

## 📄 文档

ACP 的完整文档（安装、接口定义、调用示例、使用场景）位于 **子包源文件**：

👉 **[packages/fp-acp/README.md](../../packages/fp-acp/README.md)**

## ❓ 交互问答（ask_user → deferred）

ACP 无带内回复通道，`ACPIO.ask_deferred = True`：

- `ask_user` 工具调用时，问题（含 `options` 编号列表与 `suggest` 推荐值）被
  推送为一条 `agent_message_chunk` 展示在 IDE 会话中，`ask()` **立即返回空串**；
- 编排层识别 deferred → 工具返回 `{"status":"deferred","note":"…下一条消息即为其回答"}`，
  **不注入、不回执**，LLM 结束本轮等待；
- 用户在 IDE 对话框的**下一条消息**即其回答，自然走新轮次 user 消息（权威注入，I2）。

> 旧实现 `ask()` 返回 `"q"`，会被当成用户回答注入上下文 —— 伪造人类发言，
> 已修复（测试：`test_ask_user.py::test_ask_deferred_no_injection`）。

## 🔗 快速链接

- [安装与使用](../../packages/fp-acp/README.md#快速使用)
- [JSON-RPC 接口](../../packages/fp-acp/README.md#json-rpc-接口)
- [调用示例](../../packages/fp-acp/README.md#调用示例)
- [使用场景](../../packages/fp-acp/README.md#使用场景)

## 🔄 reload 与会话续接

ACP 实例经 `reload` 热重启后自动恢复会话，并按 handoff 的 `kind` 分流
（`fp-acp` 的 `main()` 首行已捕获启动命令快照 `FP_LAUNCH_JSON`——新入口契约
第 0 步 `capture_launch_command()`，故直启 `fp-acp` 亦可 execve 重启）：

- **kind=tool**（LLM 携动态口令发起）：自动续接对话，续接完成后为挂起的
  `session/prompt` 请求补发 chunk 通知与带 `"stopReason": "end_turn"` 的 result；
- **kind=command**（用户输入 `/reload`，人触发免口令）：不进续接，仅恢复会话
  并显示一行完成提示，同样为挂起请求补发 result（否则客户端悬等）。

两种 kind 客户端都无需重发请求。
