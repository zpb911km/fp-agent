# Zeta 邻居领域模型（P0 草案 v0.2）

> 状态：草案 v0.2 — 已吸收双路外部评审（设计路 + 红队路）的对撞结果
> 分层门禁：本模块的 import 图中**不得出现** a2a / httpx / socket / 任何 codec 与传输库。
> 验收判据：删掉全部 codec 后，本层测试必须全绿。
> 信任红线：**LLM 的输出不是可信输入。** 凡进入本层的状态（契约、卡、协商结论），
> 必须存在一条非 LLM 的确定性校验路径——信任根不许放在任何实例的嘴里。

## 0. 定位

Zeta = 协议翻译网关（路由器），不是隧道。

```
领域模型（本文件，协议无关）         ← IR：只承载自描述数据，不承载传输语义
   ↑↓
Codec 层（a2a_codec / fp_direct_codec / ???，可插拔，一邻居一档）
   ↑↓
传输层（localhost / tcp / frp，可插拔）
```

对等原则：无上下级；任一方可主动；不同时在线（邻居搬家无所谓）；
对话动机在实例自己的 LLM 肚子里，不在任何编排器参数里。

**两个协商必须分开**（v0.2 修正 v0.1 的混淆）：
- **Profile 协商**（机械层，LLM 不参与）：谈"我们用什么语言"——codec、降级链；
- **契约协商**（语义层，LLM 参与但受 hash+schema 双锁）：谈"我们合作的内容"——API 契约。

## 1. 邻居表条目 NeighborCard

发现：同机共享目录 `~/.local/share/fp/peers/<name>.json`。
**每实例只许写自己的卡**（O_EXCL 创建自己的文件，不碰别人的）；读取方验签 +
比对 card_hash + 校验心跳新鲜度。跨机时 endpoint 换 frp 地址，卡不变。

```python
@dataclass
class NeighborCard:
    name: str                     # 邻居唯一名（参与同时发起的字典序裁决）
    kind: str                     # FP_INSTANCE | EXTERNAL_A2A_AGENT
    # ── 业务身份（路由依据，v0.3 新增）──
    cwd: str                      # 工作目录（一目录一实例 → cwd 即身份键，见 §1.5）
    business: str                 # 主要业务/职责（自然语言，路由的核心依据）
    tags: list[str]               # 业务标签："backend"|"frontend"|"research"|"infra"|...
    # ── 运行状态（v0.3 新增）──
    status: str                   # idle | busy | away
    current_task: str | None      # 当前任务摘要（打电话的人据此判断忙不忙）
    started_at: float
    # ── 技术能力 ──
    endpoints: list[Endpoint]     # 多端点 + priority：local:// | tcp:// | frp://
    codec: str                    # 首选 codec："a2a" | "fp_direct"
    capabilities: Capabilities    # 见下
    roles: list[str]              # 自我声明立场（非指派）："backend", "frontend", "verifier"
    trust: TrustLevel             # TRUSTED_PEER | KNOWN | STRANGER（见 §1.1）
    heartbeat: float              # unix ts；offline_threshold = 3× 心跳周期
    card_hash: str
    sig: str | None               # 卡签名（STRANGER 互访强制）
    issued_at: float              # 过期卡自动失效（防 stale 记录）
```

```python
@dataclass
class Capabilities:
    profiles: list[str]           # 支持的 profile："fp-contract-profile", "bare-a2a", ...
    semantic_layers: str          # MECHANICAL_ONLY | SEMANTIC_CAPABLE
    max_envelope_bytes: int | None
    supports_idempotency: bool
    # 注：不声明角色与意图，只声明"我接得住什么"——意图是会话期消息的事
```

### 1.1 信任档与安全闸门

| trust | 跨进程消息进 prompt | 危险工具（bash/edit） | 协商模式 |
|---|---|---|---|
| TRUSTED_PEER（同机熟人） | 标注来源 + 长度上限 | 默认可用 | 完整 hash+schema 共识 |
| KNOWN（互访过） | 标注来源 + 长度上限 | 提级确认 | 完整 hash+schema 共识 |
| STRANGER（裸外部） | 强制边界标记 + 严格上限 | 强制确认/白名单 | 降级：schema 校验兜底 |

规则（v0.2 吸收）：
- **信任升级只能由本地策略决定**，不能凭对端声明——"连续 N 条消息无异常"这类部署策略配置；
- 信任标签由**发送方填写、接收方覆盖**（接收方永远有权降级）；
- **降级是降带宽，不是降信任检查**——降级模式同样要过 §1.2 的会话期验卡。

### 1.2 发现目录的防投毒（红队 #2 的解）

1. 每实例只写自己的文件，写入原子替换（临时文件 + rename），防半写读取；
2. 卡签名 + issued_at：读取时验签、比 card_hash、过期即失效；
3. **每次会话建立时重新验卡**，降级模式不允许绕过此步——防止 stale 卡把
   会话引向已变更能力（或已被顶替）的对端；
4. 目录是**发现缓存，不是信任源**：真正的信任判定在本地 trust 档 + 会话期校验。

### 1.3 发现必须告知 LLM（功能优先）

**问题**：若邻居发现只发生在 Zeta 层（机械层），双方的 LLM 根本不知道对方存在，
就不会产生「找他商量」的动机——**发现等于没发现**。

**规则**：**发现事件要注入 LLM**（与铃声同通道，环顶注入）：

```
【邻居】peer:B 上线（能力=contract/query，端点 local://B）→ 可用 peer_list 查看
```

- 新邻居首次上线 → 注入一次简报；
- 邻居离线 / 换端点 → 可选注入；
- 提供 `peer_list()` 工具，LLM 可随时查邻居表。

**边界（修正 v0.2 的机械/语义二分）**：
- 协议层的机械消息（profile_probe 等）**仍不进 LLM**（那是网关自己的握手）；
- 但**发现结果**（谁在线、有什么能力）是**语义事件**，必须进 LLM。
- **功能优先**：不为「防打扰」牺牲「能发现」——宁可多注入一条简报。

### 1.4 名片即路由表（功能优先）

**问题**：若名片只有技术能力（profiles/codec），打电话的人无法知道「该找谁」——
只能把请求群发给所有邻居，**把所有人骚扰一遍**。

**解法**：名片携带**业务身份**——`workspace` / `business` / `tags` / `status`。打电话的人
**先 `peer_list()` 拉全部名片，本地筛选出「对的人」，再单发**——零骚扰。

```
【邻居】peer:B 上线
  ws = repo://.../agent_new_arch    business = "FP 平台开发（core + 协议）"
  tags = [backend, infra]           status = busy
  当前 = Zeta 契约 schema 设计
```

- 路由依据优先级：`tags`（我能对应）> `business`（我读得懂）> `workspace`（同一项目的实例）；
- `status=busy` → 打电话的人可据此决定「现在发还是待会发」（配合铃声 / 离线留话）。

**`business` 从哪来**（功能优先的实用路径）：
1. 启动声明（`fp --as "后端"`）——最可靠；
2. 或从 `cwd` 推断（项目名 / README 首行）；
3. 或由实例 LLM 开工后自我更新名片（`peer_set_business()` 工具）。

先支持 1+2，第 3 项是增强。

### 1.5 一 workspace 多实例：身份锚点是会话 sid（v0.5 修订）

> 本节 **v0.5 整体替换** 原「一目录一实例：cwd 即身份键」。完整论证见
> `zeta_身份与实例模型.md`。

**根因**：原设计把「位置 / 实例 / 身份」强绑为 1:1:1（`workspace ≡ instance ≡
identity ≡ cwd`），在「同项目并行多 agent」时集中暴露：换目录 → `.fp/memory` 分叉
→ 记忆割裂；`name = basename+pid` → reload 换名、路径同名冲突。

**解法（三分解耦）**：
1. **workspace : instance = 1:N** —— `.fp/zeta/instances/<name>.json` 每实例一登记
   （替代单文件 `.fp/zeta.instance.json` 单锁）。同 workspace 多实例**天然共享**
   `.fp/memory` 与 `.fp/tasks.json`（本来就是 workspace 级）。`FP_ZETA_SINGLE=1`
   保留旧独占语义。
2. **身份锚点 = 会话 sid** —— `name = <workspace名>-<sid短handle>`
   （handle = `sha256(sid)[:8]`，不可逆、不泄露本地 sid）。sid 全局唯一 → 名唯一；
   reload/resume 保持同 sid → 名稳定。`--as` 仅是可选显示昵称（写进 business），
   **永不是身份键**。
3. **路由键 = workspace**（§1.4），cwd 降为进程属性（不参与路由）。

**冲突语义不变（降级可见）**：仅**同名**（同 sid）冲突——不同 sid 的实例可同 workspace
共存。冲突时第二个实例 fp 照常启动、**不启用邻居面**，但 `peer_*` 工具**照常注册**、
调用返回明确原因、system prompt 追加「本实例邻居面当前不可用——<原因>」。

理由：这是「发现必须告知 LLM」的同一条原则——**静默消失等于能力不存在**。
**显式关闭**（`FP_ZETA_DISABLE=1`）另当别论：用户明确不要 → 不注册工具、不注入说明。

**副作用与边界**：
- 同 workspace 多实例共享记忆/任务（正是目的），要隔离就用不同 workspace（目录）；
- resume 跨目录 → 人格（会话）与位置（workspace）错配：workspace 跟启动目录，
  首次错配经 system prompt 提示一次（E(ii)）；
- 僵尸清理：登记带 pid，启动时 `sweep_stale` 回收进程已死的登记。

## 2. 消息信封 Envelope

```python
@dataclass
class Envelope:
    id: str                       # 全局唯一（uuid/ULID）
    correlation_id: str           # 同一议题线程共享
    reply_to: str | None          # 回复链
    idempotency_key: str | None   # 幂等键：重试/重放共享；去重窗口 ≥ TTL（建议 24h）
    from_: str
    to: str                       # 单播；广播 = 多次单播
    kind: MsgKind                 # MECHANICAL | SEMANTIC
    ttl: Ttl                      # {deadline, max_hops} —— 取先到者；MVP 单跳，max_hops 预留
    sent_at: float
    payload: Payload
    codec_meta: dict | None       # codec 专属元数据，IR 视为不透明 kv，不解释
    sig: str | None
```

**kind 分流是成本与安全的双闸门**（不变）：

- `MECHANICAL`：**MUST NOT 注入 LLM**，Zeta 层直接应答——profile 协商、
  ack/hash 确认、心跳、离线回执、超限通知；
- `SEMANTIC`：才注入实例（propose、counter、clarify），走 `portal.run.send`。

```python
@dataclass
class Payload:
    kind_mech: str | None         # MECHANICAL 用："profile_probe" | "ack" | "nack" | ...
    # ── SEMANTIC 用 ──
    topic: str | None             # "contract" | "task" | "chat"
    body: str | None              # 语义文本
    parts: list[Part] | None      # Artifact 多 part：text/file/data，非裸 string
    injection: InjectionHint | None

@dataclass
class InjectionHint:
    inject_as: str                # USER_MESSAGE | SYSTEM_CONTEXT | TOOL_RESULT | NOTIFICATION
    trust_label: str              # TRUSTED_PEER | UNTRUSTED_CONTENT（生成规则见 §1.1）
    max_context_tokens: int | None
```

注入管线硬规则（v0.2 强化）：
- `UNTRUSTED_CONTENT` **MUST** 走 USER_MESSAGE，用显式边界标记包裹
  （如 `<untrusted_peer_content>...</untrusted_peer_content>`），SHOULD 过注入检测；
- 每次注入记审计日志：envelope_id / sender / trust_label / inject_as。

## 3. Profile 协商（机械层，LLM 不参与）

```
NOT_STARTED ──probe──▶ PROBING ──交集+降级链──▶ NEGOTIATED / DEGRADED
                              └──无交集──▶ FAILED（标 STRANGER，不投语义消息）
```

- 双方各自声明 `profiles` 列表，取**交集**选 priority 最高者；无交集走
  `downgrade_to` 降级链；
- **同时发起裁决**：互发 probe 时，比较 name 字典序，小者主导、大者应答——
  防 probe 风暴，去中心系统里"谁让步"必须是确定性规则而非竞争；
- 降级表：`DEGRADED+MECHANICAL_ONLY` → 语义消息降为通知（不含正文）；
  `FAILED` → 不投递，只留日志。

### 3.1 实现落点（n11 裸 A2A 降级）

- **识别**：`a2a_codec.card_from_a2a()` 把标准 A2A AgentCard 映射为
  `NeighborCard(kind=EXTERNAL_A2A_AGENT, codec="a2a")`。任何 A2A 对端天然具备
  `bare-a2a` 底线能力；若其 `capabilities.extensions` 带 `urn:fp:zeta:profile:...`
  才额外获得 `fp-contract-profile`（那本身也是 FP）。`verify_card` 放行外部卡的
  空 profile（FP 实例仍强制声明），外部卡可被 `scan` 正常发现。
- **降级判定**：`PeerService.negotiate(name)` / `is_degraded(name)` 接上领域层
  `negotiate_profile`。**未知来源一律视为降级**（保守）：不认识的对端不享受对等双锁。
- **UNVERIFIED 标注**：接收侧 `PendingCall.degraded` 在 `poll()` 时按对端档位判定；
  `answer()` 输出 `[机器校验·UNVERIFIED]`——**本地 schema 校验照做**（降级只降带宽，
  不降信任检查），但如实声明「对方没有同等的机器校验保证」。
- **信任**：`a2a_message_to_envelope()` 把外部入站一律标 `UNTRUSTED_CONTENT`——
  接收方的信任判断不由发送方写入。
- **可见性**：`neighbor_brief` 对外部对端标注「⚠外部A2A对端·将 UNVERIFIED」，
  识别结果对 LLM 可见（否则「能跟非 FP 说话」等于没说）。
- **已落地**：真实 HTTP 传输见 §3.2。

### 3.2 HTTP 传输：A2A v1.0 wire（实测基线）

`a2a_http.A2aHttpClient` —— 取卡 + 发消息，同步（peer 层本就同步轮询）。wire 形状
由 `a2a_codec` 决定，本模块只搬运与错误归一。

**A2A 1.0 是破坏性升级**（2026-10 实测 against `a2a-sdk==1.1.0` 官方 helloworld），
与 0.x wire 多处不兼容，已显式编码：

| 维度 | v0.x | v1.0（实测） |
|---|---|---|
| card 路径 | `/.well-known/agent.json` | `/.well-known/agent-card.json` |
| card 端点 | 顶层 `url` | `supportedInterfaces[].url` |
| 方法名 | `message/send` | `SendMessage` |
| 版本协商 | 无 | HTTP 头 `A2A-Version: 1.0`（**必需**） |
| role | `user` / `agent` | `ROLE_USER` / `ROLE_AGENT` |
| part | `{kind:"text", text}` | `{text, mediaType}`（扁平） |

**出站按对端 `protocolVersion` 选形状；入站解析一律宽松（两种都认）**——这正是
「协议版本也是能力的一部分」的落地。

**验证**：`test_zeta_a2a_http.py` 16 项（wire 兼容 + MockTransport + 错误归一）；
`ZETA_A2A_E2E_URL` 门控的真实 e2e 对官方 helloworld 取卡并收到
`Hello, World! I have received your request (…)`。

## 4. 契约协商 fp-contract-profile（语义层，双锁）

落点在交换区，不在线上对话里——**对话可断可丢，契约不能**。

```
<workspace>/.fp/contracts/<name>.contract.json
写权：提议方写（O_EXCL），确认方只读校验；已存在比 hash，不符 = 拒绝而非覆盖。
```

状态机（correlation_id 绑定一条协商线）：

```
INIT ──propose(schema_ok, hash)──▶ PROPOSED ──ack(hash)──▶ AGREED
 ▲                                   │ │
 │         counter(hash')            │ └─nack(reason)──▶ REJECTED
 └───────────────────────────────────┘
轮数上限 N（默认 3）→ 超限 FAILED → input-required 转人类
离线 → per-peer outbox（带 TTL），上线重投；发起方拿立即 OFFLINE 回执自决去留
```

### 4.1 双锁 = hash 锁字节 + schema 锁语义（红队 #1 的解，本文件最重要的一节）

**hash 只保证双方引用同一字节串，不保证字节串是对的。** 两个 LLM 可以
"合谋式收敛"到同一份有语义漏洞的契约——hash 会愉快地锁定这个错误。
所以：

1. **契约必须是结构化 schema，自由文本只做注释**：权限、路径、副作用、
   端点定义必须落在机器可校验的字段里——LLM 不得用散文定义安全边界
   （"允许 tmp/* 写入" 这类歧义在 schema 层直接非法）；
2. **落盘前跑确定性 conformance check（非 LLM）**：schema 校验 + 规范化
   （路径绝对化、端点 URL 规范形式）+ 与双方已声明能力比对；不过 = 不落盘；
3. **轮数上限 + 质量下限**：最后一轮仍有未消解的 schema 违规 → 宁可
   AGREED 不成立转 FAILED，也不许"赶在上限前妥协"——轮数压力会诱导
   LLM 放弃精确性换收敛；
4. hash 不等 = 没谈成，**无论双方 LLM 说了多少个"就这么定"**（不变的红线）。

> 一句话：**LLM 只产候选，一致性由 hash 判，正确性由 schema 判。**
> 这是把信任根从 LLM 手里拿回来的唯一便宜手段。

### 4.2 运行期兜底

语义分歧在运行期暴露（联调才炸）时无上级仲裁 → 协商作废整条重来；
因此 §4.1 的机器校验必须前置到落盘前，**不能指望运行期发现问题**。

## 5. 离线 / 重复 / 同时发起（三个经典边界）

| 边界 | 规则 |
|---|---|
| **离线投递** | 发送方 MUST NOT 阻塞：入 per-peer outbox，at-least-once；expires_at 到期丢弃（建议 7 天）；对方上线扫描 outbox 按序处理后回 ack，发方才删。**邻语义：拿立即 OFFLINE 回执，自主决定等待或默认先干。** |
| **重复消息** | 幂等窗口去重，保留 ≥ TTL（建议 24h）；见过即返回缓存；处理中撞见即冲突错误不重跑。机械消息天然幂等无需键。 |
| **同时发起** | Profile 协商：name 字典序裁决主导权（§3）。语义消息：双向并发各自独立处理，correlation_id 区分线程——**对等网络允许同时说话，这正是"邻居"的本义。** 共享文件的写冲突不由本层解决（应用层职责），交换区写权规则（§4）是本层唯一的写仲裁。 |

### 5.1 实现落点（P3-a，已落地）

**去重键 = `(from_, idempotency_key 或 信封 id)`**：`from_` 提供命名空间（不同发送方的同 key 不碰撞）；
无显式 `idempotency_key` 时以**信封 id 兜底**——同一封信被重投/重放也只处理一次。
这是 `IdempotencyWindow` 从「定义了但未接线」到真正接入 `poll()` 的收口。

| 机制 | 实现 | 位置 |
|---|---|---|
| 幂等去重 | `poll()` 前置闸门：`check(key)` 非 None（cached / inflight）即跳过；未见则 `remember(key)` | `peer.py` |
| 过期丢弃 | `send()` 给语义消息设默认 TTL 7 天；`poll()` 检查 `ttl.expired()`——过期留话**收到但不处理** | `peer.py` |
| 存储卫生 | `DirTransport.purge_processed()` 按 mtime 回收 `.processed/` 历史；`housekeeping()` 节流（≥300s）驱动 | `transport.py` · `peer.py` |

> **与愿景的差异（诚实标注）**：当前 MVP 的 `DirTransport` 直接写对方 inbox，**投递即送达**（文件写原子），
> 故无「独立 outbox + 发方收到 ack 才删」的重发机制；at-least-once 只由「重放同一信封」触发，由幂等窗口兜底。
> 跨网络 transport（frp / Tailscale）需要重试时，**复用同一 `idempotency_key` 即可走同一条去重路径**——机制已就位，等传输层接入。

测试：`test_zeta_peer.py` 幂等小节 +7（同 key 去重 / 信封 id 兜底去重 / 不同消息各自处理 / 过期丢弃 / 默认 TTL / purge / 节流）。

## 6. 任务/状态映射（a2a_codec 侧）

| 领域 | A2A | 备注 |
|---|---|---|
| WORKING | working | 远程 run 进行中 |
| NEED_INPUT | input-required | 契约协商等人 / 等对方 |
| DONE | completed + Artifact | |
| FAILED | failed | 轮数超限/校验不过/对方拒绝 |

- **A2A 是委派语义，FP profile 是协商语义**——wire 形状借 A2A
  （message/send 信封装协商消息），语义自定义在 profile 里；
- 关联防张冠李戴：Zeta 串行化远程驱动（同实例同时最多 1 个 remote run）
  + 按邻居分 session + correlation_id 时间窗界定；
- codec 映射锚点（IR 不感知字节）：envelope_id→JSON-RPC id、
  idempotency_key→HTTP header、correlation_id→task.contextId、
  payload.mechanical→notification、payload.semantic→message/send params。

## 7. 注入冲突与资源闸门（红队 #3 的解）

- `_active_io` 单槽 → Zeta 的 io 用**记日志不渲染**的专用通道；
- **人类优先必须是抢占式的，不是排队式的**：
  - 工具循环的每个工具调用边界 = 可中断点：高优先级本地请求可挂起远程任务
    （状态快照 + 释放），而非排队等远程自然收敛；
  - 队列层人类插队只是第一道，**中间态可抢占**才是第二道（v0.1 只有第一道）；
- **远程注入硬资源预算**：时长 / 工具调用数 / 文件写入量，耗尽强制让路——
  外部对端的协商策略不得决定本地用户体验（防"先占循环再慢慢协商"的
  协议内 DoS）；
- 跨进程消息 = 二跳注入面：origin 标注 + 长度上限 + trust 档决定确认门槛。

#### 7.1 铃声机制：被寻找方如何感知「有人找」

**问题**：LLM 实例在跑自己的任务循环，网关收到远程消息后，实例不会立刻知道有人在等——
它可能埋头干很久（甚至长工具不返回就一直不知道）。

**机制**：不打断、只提示、由 LLM 自主决定何时接——像电话铃声。

**落点：core 环顶注入通道 + 唤醒级标记（wake）**

- `core/jobs.py`：`inject_event(kind, content, wake=True)` 入队 → 每轮工具循环回环顶
  `drain_ready()` 取走 → 以 **user 角色**消息注入（不变量 I2/I3：协议配对完好、
  shortcircuit degenerate 不删）；
- 注入发生在**两个 LLM 调用之间**（工具轮回环顶），下一轮 LLM 可见，**不打断当前动作**；
- **`wake=True` 补齐「实例空闲时无人 drain」的缺口**：实例若完全空闲（没有进行中的
  轮次），环顶永不出现，注入就永远躺在队列里。标记 wake 后，core 的**空闲泵**会在
  实例空闲时主动起一轮消费它——**空闲的邻居也会被铃声叫醒**，而非只能「留话到下次活动」。

**空闲唤醒链路（问题 2 路线：由被叫方 core 自己唤醒，非 Zeta 启动 core）**

| 层 | 落点 | 职责 |
|---|---|---|
| core · jobs | `inject_event(..., wake=True)` / `has_pending_wake()` | 消息携带唤醒级标记 |
| core · agent | `process_wakeup()` | 不追加用户输入、直接进循环消费注入（形状同 reload 续接） |
| core · agent | `is_run_active` | **覆盖整轮**（含工具执行）的忙碌信号——`is_processing` 只覆盖 LLM 调用期，**不可**用作「能否唤醒」判据 |
| core · portal | `run.wake()` + 空闲泵 | 唤醒入口；`ctl.open` 起泵、`ctl.close` 停；周期检查「空闲 + 有 wake 注入」即消费 |
| zeta · peer | `self._inject("peer_ring", ring, wake=True)` | 铃声即唤醒级事件 |

要点：唤醒由**被叫方的 core** 完成（`portal.run.wake` → `agent.process_wakeup`），
Zeta 只负责「把铃声标记为 wake」——不碰 session / IO / run。带外输出走 EventBus
（webui）或 `_default_io`（terminal），无需额外通道。

**铃声响的语义（关键：状态驱动，不刷屏）**

- 铃声消息一旦注入，只要未接听就**一直在上下文可见**——「不接就一直响」；
- 只在**状态变化**时再次响铃：新来电 / 对方取消 / 等待升级；
- **不每轮重复注入**（避免刷屏 + 上下文膨胀）——「持续可见」本身就等于「一直在响」。

**铃声内容（来电显示级，不含正文）**

```
【铃声】peer:B 在找你（主题=contract，已等 2m）→ 接听调 peer_answer
```

给最小决策信息（谁 / 大类 / 等多久），**正文不注入**——LLM 在未接听时读不到内容，
这是「暂时不告诉他内容」的落地。

**接听 = LLM 主动调工具**

- LLM 看到铃声 → 自行决定：继续手上的活（铃声留在上下文）或调 `peer_answer()` 接听；
- `peer_answer()` 取回正文 → 以 **tool result** 形式进对话（符合 I2「tool result 只
  是收据」的配对语义）；
- 接听成功 → 清该来电状态 → 铃声停止。

**与「人类优先」的分层一致（对等的体现）**

| 来源 | 机制 | 语义 |
|---|---|---|
| 本地用户 | `run.cancel` 抢占 | **能打断你** |
| 邻居来电 | 铃声注入 | **只能让你知道** |

**边界**

- 铃响时机：实例**空闲** → 空闲泵立即起一轮（≤0.25s）；实例**忙碌** → 铃声在该轮
  环顶出现（若正跑长工具，则等其返回）。「人类优先」由 `is_run_active` 守护，不打断；
- 对方取消 / TTL 到期 → 清铃声状态（可注入一条「来电已取消」）；
- 多来电 → 铃声带计数，逐条接听；
- 升级：等太久 → 提示「对方已等 Nm，可能阻塞其工作」（防永远不接）。

**架构推论（重要修正）**

要触达 core 的注入通道，Zeta 接收面最自然的形态是**插件（in-process L2）**：
可直接用 `jobs.inject_event(wake=True)` + 注册 `LifecycleHook`（如 `ON_TOOL_RESULT`
做尾随）+ 注册 `peer_*` 工具；前端则通过 **`portal.run.wake()`** 主动唤醒——唤醒
能力已外化为协议的一部分。
→ **Zeta = 一个 core 插件**（内含 A2A 通信服务 + 铃声注入 + `peer_*` 工具），
而不是「外部前端 + 插件工具」两体。

## 8. P0 验收测试

1. `test_domain_imports_clean`：领域模型 import 图无 a2a/httpx/socket/codec；
2. 删 codec 后领域测试全绿；
3. `test_mechanical_never_injected`：MECHANICAL 永不进 LLM 管线；
4. `test_contract_requires_schema_and_hash`：hash 不等或 schema 违规，不可 AGREED、不落盘；
5. `test_card_write_isolation`：实例 A 的写操作不可能覆盖实例 B 的卡；
6. `test_concurrent_probe_tiebreak`：同时发起的 profile 协商，字典序裁决收敛到单一主导；
7. `test_human_preemption`：远程循环中间态可被本地请求挂起（快照点可恢复）。

---

## 附：v0.1 → v0.2 变更摘要（评审对撞记录）

**红队路（glm）三刀，全部采纳：**
- 🔴 #1 hash = 对同一份幻觉的共识 → §4.1 双锁（schema+确定性校验+质量下限），升为全文最重要一节；
- 🟠 #2 发现目录可写投毒/半写/stale → §1.2 自写隔离+签名+会话期重验+"目录是缓存不是信任源"；
- 🟡 #3 人类优先不可兑现（循环不可抢占）→ §7 抢占式中断 + 远程硬资源预算。
- 结构性诊断采纳为文首信任红线："系统不得把 LLM 输出当可信输入"。

**设计路（deepseek）吸收：**
- idempotency_key / reply_to / codec_meta（不透明透传，IR 不解释）/ TTL 结构化；
- InjectionHint 四值 inject_as + 信任标签"发送方填、接收方覆盖"；
- Profile 协商状态机与字典序裁决；信任升级凭本地策略；
- 离线 outbox at-least-once + 24h 幂等窗口 + 7 天 TTL 的实践参数；
- codec 映射锚点表（§6）。

**裁剪（防过度设计）：**
- hop_trace 多跳环路检测 → MVP 单跳，max_hops 留字段不实现（路由器方向 P3 再说）；
- deepseek 四信任档 → 收敛为三档（MVP 够用，档位语义保留扩展空间）；
- 显式 probe/confirm 心跳消息 → 同机场景允许 mtime 隐式心跳。
