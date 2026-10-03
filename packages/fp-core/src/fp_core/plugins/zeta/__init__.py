"""Zeta 插件 — 对等协作协议（邻居模型）。

Zeta 让多个对等 agent 实例（FP ↔ FP，以及 FP ↔ 外部异构 agent）像邻居一样协作：
没有上下级、可以不同时在线、任一方都能主动找对方，靠一份共同签署的契约达成一致。

文档：docs/dev/zeta_协议总览.md / zeta_邻居领域模型.md / zeta_契约schema.md

────────────────────────────────────────────────────────────────────────────
任务图 #145 快照 —— 「Zeta 对等协作协议实现」
  目标：两个真实 FP 实例像邻居一样协作——互相发现、互相叫醒（铃声）、谈成一份
        经非 LLM 机器校验的契约（hash 锁字节 + schema 锁语义），全程无中心、
        可离线留话。
  分层门禁：领域层 import 图不得含 a2a/httpx/socket；删掉全部 codec 后领域层测试全绿。

  ✅ P0  领域模型 IR（NeighborCard/Envelope/双锁判定/幂等窗口/发现简报）…… domain.py
  ✅ P1  通信面（SharedDir 发现后端 + 名片心跳 + a2a_codec + 铃声注入 + peer_* 工具）
  ✅ P2  契约机（schema 校验器注册表 + propose/ack hash 共识 + 交换区写权）…… contracts.py
  ✅ 双实例实验 + 真实 LLM 人工验收（3 实例、谜题测试）
  ✅ core 唤醒能力（inject_event(wake=) / process_wakeup / is_run_active / portal.run.wake）
  ✅ 离线 outbox + 幂等收口 · 裸 A2A 降级 · A2A v1.0 HTTP 传输…… a2a_http.py

  ⬜ 遗留 n14  send 路由接线：peer_send 按邻居卡 endpoint.kind 分派
              （a2a → A2aHttpClient HTTP / local → 文件收件箱）
              —— A2aHttpClient 已能独立对任意 A2A agent 说话，只差分派。见 peer.py TODO(n14)。
  ⬜ 遗留 n12  跨网络发现/传输后端（Tailscale/WireGuard 优先、frp 兜底；方案已存档，暂缓实现）
              —— 见 transport.py / discovery.py 的 TODO(n12)。
────────────────────────────────────────────────────────────────────────────
"""

from .plugin import ZetaPlugin

__all__ = ["ZetaPlugin"]
