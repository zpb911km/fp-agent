# ACP 协议

> ACP（Agent Communication Protocol）是本项目的 JSON-RPC 2.0 远程调用协议，
> 由 `fp-acp` 子包实现。

## 📄 文档

ACP 的完整文档（安装、接口定义、调用示例、使用场景）位于 **子包源文件**：

👉 **[packages/fp-acp/README.md](../../packages/fp-acp/README.md)**

## 🔗 快速链接

- [安装与使用](../../packages/fp-acp/README.md#快速使用)
- [JSON-RPC 接口](../../packages/fp-acp/README.md#json-rpc-接口)
- [调用示例](../../packages/fp-acp/README.md#调用示例)
- [使用场景](../../packages/fp-acp/README.md#使用场景)

## 🔄 reload 与会话续接

ACP 实例经 `reload` 热重启后自动恢复会话，并按 handoff 的 `kind` 分流：

- **kind=tool**（LLM 携动态口令发起）：自动续接对话，续接完成后为挂起的
  `session/prompt` 请求补发 chunk 通知与带 `"stopReason": "end_turn"` 的 result；
- **kind=command**（用户输入 `/reload`，人触发免口令）：不进续接，仅恢复会话
  并显示一行完成提示，同样为挂起请求补发 result（否则客户端悬等）。

两种 kind 客户端都无需重发请求。
