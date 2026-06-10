"""
Five Pebbles — ACP (Agent Client Protocol) Server

将五块卵石作为 ACP Server 接入 VS Code / Zed 等 IDE，
让编辑器直接驱动 AI Agent，无需复制粘贴。

协议: JSON-RPC 2.0 over stdio
参考: https://github.com/agentclientprotocol/agent-client-protocol
"""

from app.acp.server import ACPServer, main

__all__ = ["ACPServer", "main"]
