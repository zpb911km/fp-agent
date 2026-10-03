# fp-contract-profile：协作契约 Schema（草案 v0.2）

> 配套：`zeta_邻居领域模型.md` §4。这是双锁（§4.1）的"锁"本体。
> v0.2 已吸收双路外部评审（独立设计路 + 演进红队路）的对撞结果。
>
> 设计目标：**表达力开放 + 机器可校验 + 可演进而不断旧对端**。
> 一句话方针：**核心只增不改且可收缩，扩展自带唯一身份，兼容由机器判。**

## 0. 防债原则（每一条对应一种未来要还的债）

前瞻性的本质**不是堆字段，而是给"未来的变化"预留低成本的轨道**。三条主轨：

- **版本轨**：双版本 + 机器判兼容 + expand-contract（§2、§7）
- **命名轨**：URI 命名空间让扩展自带全局唯一身份（§2.2）
- **校验轨**：校验器注册表，新 kind / 新规范版本不改旧校验器（§10）

| # | 设计决策 | 预防的债 |
|---|---|---|
| 1 | 元版本与内容版本分离 | schema 语义升级污染业务数据 |
| 2 | 核心字段 append-only | 字段语义被静默篡改 |
| 3 | **expand-contract 收缩协议** | append-only 只增不减 → 契约臃肿、不敢演进 |
| 4 | 接口 `kind` 开放枚举 + 各自子 schema | 过度特化（只为 HTTP 而设） |
| 5 | 扩展用 **URI 命名空间** + "可忽略"准入 | 扩展点滥用成私有方言分叉 |
| 6 | 规范化序列化（RFC 8785 JCS） | hash 永不可对齐（共识的前提） |
| 7 | **default/nullable 必须显式** | 缺失字段含义各凭猜测（JCS 治不了的洞） |
| 8 | 未知字段分区（安全区 fail-closed） | 向前兼容把安全校验绕过去 |
| 9 | 兼容性由机器计算而非声明 | LLM 谎报"这是兼容变更" |
| 10 | 生命周期 + 废弃窗口 | 僵尸契约无人敢删 |
| 11 | 校验器注册表（schema 与实现解耦） | 校验器与 schema 版本纠缠 |
| 12 | 组合引用强制内联快照 | 引用循环 / 加载顺序地狱 |
| 13 | 组合子 schema 禁用 `additionalProperties:false` | 逻辑上死亡的空 schema |

## 1. 顶层结构

```jsonc
{
  "contract_version": "1.0",      // 元版本：本 schema 规范自身的版本
  "id": "ctr-01J...ULID",         // 契约实例唯一 id
  "name": "items-api",            // 人类可读
  "revision": 3,                  // 内容修订号（业务演进，单调递增）
  "status": "active",             // draft|proposed|active|deprecated|retired

  "parties": [                    // ≥2，对等——有意用数组而非 proposer/confirmer 二元
    { "name": "instance-A", "role": "backend" },   // role 自我声明，非指派
    { "name": "instance-B", "role": "frontend" }
  ],
  "negotiated": {                 // 协商达成的能力集合（profile 协商的产物）
    "schema_min": "1.0",
    "required_features": [],
    "optional_features": []
  },

  "scope":        { /* §4 资源作用域 */ },
  "types":        { /* 共享类型，JSON Schema 片段，可内部 $ref */ },
  "interfaces":   [ /* §3 接口，开放 kind */ ],
  "governance":   { /* §5 变更/生命周期规则 */ },
  "compat":       { /* §7 兼容性声明 + 机器判定 */ },
  "conformance":  { /* §8 符合性测试引用 */ },
  "integrity":    { /* §9 规范化 + hash + 签名 */ },

  "urn:fp:ext":   { /* §2.2 扩展命名空间，可忽略 */ }
}
```

`parties` 用数组而非 `proposer/confirmer` 二元，是前瞻性决策：今天两个实例，
明天三个（加一个 verifier 实例）不改结构。**裁决：拒绝把 parties 限死为 2**
（评审方案里 `maxItems:2` 是它自己埋的债——A2A 的 client/remote 二元不对称
不要在契约层复刻）。**但对等签名门槛建议保留"全体签署"语义**（§5）。

## 2. 版本与命名空间（前瞻性的地基）

**双版本**：
- `contract_version`：规范本身版本。解释器据此选择**校验器与解析规则**；
- `revision`：契约内容版本。业务修订递增，与规范版本正交。

演进承诺：
- `contract_version` 语义化 `major.minor`：**minor 只增字段（向后兼容），
  major 可改语义（不兼容，需显式协商迁移）**；
- 契约一旦 `active`，其**核心字段集 append-only**——任何语义修改必须新开字段，
  不许复用旧字段。这是给未来对端最硬的一句承诺。

### 2.1 append-only 不是终点：expand-contract 收缩协议（v0.2 关键补充）

**只增不减的契约最终会臃肿到没人敢动——"永不破坏"本身是债。** 删除一个字段
需要一条安全路径，这就是 expand-contract（扩展-收缩）三段：

```
① 扩展：新版加新字段（旧字段保留，标 deprecated），全体对端可继续读旧字段
② 迁移：观察期内新老字段并存（双读双写），对端逐个迁移
③ 收缩：确认所有 parties 已迁移 → 新 revision 移除旧字段（minor→major）
```

关键：**收缩（删除）必须由对端迁移状态驱动，不是由时间驱动**。契约里
`deprecations[]` 记录每个待删字段的迁移进度——把"我觉得大家都升级了"变成
可查的事实。这与 §7 的机器判兼容衔接。

#### 2.1.1 `deprecations[]` 结构（定死）

```jsonc
{
  "deprecations": [
    {
      "id": "dep-0007",                       // 契约内唯一，单调递增（审计序号）
      "target": {
        "pointer": "/interfaces/0/http/path", // RFC 6901 JSON Pointer 定位
        "was": "/api/v1/items"                // 废弃前的值（审计留痕）
      },
      "change": "rename",                     // removal|rename|semantic_change|relocation
      "reason": "路径规范化，去掉 v1 前缀",
      "replacement": {                        // 迁移目标；removal 类为 null
        "pointer": "/interfaces/0/http/path",
        "is": "/api/items"
      },
      "announced_at": "2025-06-01T00:00:00Z",
      "remove_in": { "contract_version": "2.0", "revision": 5 },  // 计划移除点
      "window_days": 30,
      "migration": [                          // 每个 party 一行迁移进度
        {
          "party": "instance-A",
          "status": "migrated",               // pending|migrating|migrated|blocked
          "at": "2025-06-20T00:00:00Z",
          "evidence": {
            "kind": "conformance",            // declared|runtime_probe|conformance
            "test_ref": "test://A/tests/test_v2_path.py::test_path",
            "test_hash": "sha256:<test-bytes>",
            "result_hash": "sha256:<result>"  // 任意方可独立复跑验证
          }
        },
        { "party": "instance-B", "status": "pending", "due": "2025-07-01T00:00:00Z" }
      ],
      "state": "migrating"
    }
  ]
}
```

**状态机**：

```
announced ──▶ migrating ──[全体 migrated 且证据达强级]──▶ ready_to_remove ──▶ removed
    │                                                            │
    └─────────────[协商撤销]──▶ withdrawn ◀───────────────────────┘
```

- `removed` 后条目**不删除**，只标 `state:removed` + `removed_at`——保留完整审计链
  （`deprecations[]` 本身也是 append-only）；
- `withdrawn`：任一方迁移中提出异议且全体同意 → 撤销废弃，目标字段恢复正常。

**收缩判据（把"可查"落到实处）**：

1. **全体 parties 的 `status == migrated`**——只要有一方 `pending/migrating/blocked`，
   闸门关闭。**`remove_in` 到点但未达标 → 不移除**（早到不删）；
   已达标但晚于 `remove_in` → 安全（晚到只是延后，不阻塞）；
2. **证据必须达"强级"**：三档——
   `declared`（对端自称，弱，不可作收缩依据）、
   `runtime_probe`（行为指标：新字段有调用、旧字段零调用持续 N 天，中）、
   `conformance`（可被任意方**独立复跑**的符合性测试 + 结果 hash，强）。
   收缩**只认强级**——"LLM 不可信"原则在演进流程里的又一次落地；
3. **`replacement` 校验**：目标指针必须解析到当前**非 deprecated** 的字段
   （防"A→B、B 又废弃"的链式塌陷，让迁移有确定落点）。

**与 §7 机器判兼容的联动**：经过**完整 expand-contract 且全员证据强级**的字段移除，
机器判定器判为 **compatible（可在原 major 内进行）**——已无对端引用，不构成破坏。
反之，未经迁移流程的移除一律判 **breaking**（须 major 升级）。
**判定依据是 `deprecations[]` 里的证据，不是提交方的声明。**

### 2.2 命名空间（决定谁能扩展、扩展如何被对待）

| 层 | 命名 | 演进权 | 未知字段策略 |
|---|---|---|---|
| 核心 | 保留名（如 `interfaces`） | 规范，append-only + expand-contract | 见 §2.3 |
| 扩展 | **URI 命名空间**（`urn:fp:ext:*` / `https://vendor/*`） | 任意方自由加 | **MUST 忽略** |
| kind 专属 | `interfaces[].<kind>` 子对象 | 该 kind 的规范 | 同核心策略 |

**扩展准入条件（防分叉）**：一个扩展字段合法的**唯一前提**是
**"接收方忽略它后，行为仍然正确"**。若忽略它会改变核心语义，它不是扩展，
是核心变更——必须走 `contract_version` major 升级。这条挡住"两个实例用扩展
私下交流核心语义、渐渐形成只有它们懂的方言"。

**v0.2 修正**：扩展键名用 **URI 前缀而非 `x-vendor` 短名**（评审建议采纳）——
URI 天然全局唯一、无命名冲突、可自解析归属，比裸 `x-` 前缀更抗漂移；
`urn:` 用于内网/自研，`https://` 用于公开扩展。

### 2.3 未知字段分区策略（前瞻性 vs 安全的张力）

一般向前兼容用 Postel 定律（宽容接受），但**安全相关的未知字段宽容 = 越权**：
一个未来的 `scope.deny` 新字段被旧解析器忽略，就等于没有这个禁令。

规则（fail-closed）：
- **安全命名空间**（`scope.*`、`governance.*`、`interfaces[].semantics.*`）：
  出现未知字段 → **拒绝**（宁可不达成，不可误放行）；
- **非安全命名空间**（扩展 URI、描述性字段）：未知 → **忽略**；
- 每个命名空间在规范里标注自己是 `open` 还是 `closed`。

## 3. 接口模型（开放 kind）

```jsonc
{
  "id": "get-items",
  "kind": "http",                 // 开放枚举：http|file|message|function|...
  "stability": "stable",          // stable|experimental|deprecated
  "http": {                       // kind 专属子对象，只有 kind=http 时有
    "method": "GET",
    "path": "/api/items"
  },
  "in":  { "$ref": "#/types/GetItemsReq" },
  "out": { "$ref": "#/types/ItemsPage" },
  "errors": [ { "code": 404, "when": "not found" } ],
  "semantics": {
    "idempotent": true,
    "side_effects": [],           // 声明副作用（供对方评估危险度）
    "consistency": "strong",
    "cacheable": false
  }
}
```

- 新接口类型（将来加 `grpc`、`event-stream`）= 定义一个新 kind 子 schema，
  **核心结构不动**——核心只认 `id/kind/in/out/semantics`，kind 形状外包给子规范；
- `semantics` 是安全区：被请求方的确认门槛判定依赖它，未知字段 fail-closed（§2.3）；
- `stability` 允许**渐进协商**：先谈成 `experimental`，稳定后再提级——契约不必一次谈满。

**组合纪律（v0.2 补充）**：除非是最终封闭对象，子 schema **禁用
`additionalProperties: false`**——组合两个各自封闭的子 schema 会得到
"逻辑上死亡的空 schema"（验证器能过，但没有数据能通过）。封闭性只在最外层声明。

## 4. 资源作用域（治红队 #1 的 `tmp/*` 歧义）

```jsonc
{
  "resources": {
    "default_effect": "deny",              // 默认拒绝（fail-closed，v0.2 显式化）
    "read":  ["repo://server/src/**"],
    "write": ["repo://server/api/**"],
    "deny":  ["repo://**/secrets/**"]      // deny 恒优先，不可被扩展豁免
  },
  "norm": "path-posix-absolute-v1"         // 规范化规则引用（§9）
}
```

硬规则：
- 路径必须是**规范化形式**，schema 校验器强制（不接受 `tmp/*` 这类相对/歧义
  写法——必须 `repo://.../tmp/**` 绝对形式）；
- `default_effect: deny` 显式声明"没写的就是不许"——比"列出所有允许"更抗遗漏；
- `deny` 恒优先，且**不可被扩展字段豁免**（安全区，§2.3 fail-closed）；
- 作用域是契约里**唯一**的权限声明处——接口语义不得另行隐式授权。

（评审建议的 scope 可扩展性：`target.type` 开放枚举 `file|dir|s3|http|queue|
function|custom`，为未来接入对象存储/消息队列留位——采纳，具体形状 kind 化。）

## 5. 变更规则与生命周期（governance）

```jsonc
{
  "governance": {
    "change_policy": "append-only-core",
    "amend_requires": "all-parties",     // 修订需全体签署（对等：无人可单方改）
    "deprecation_window_days": 30,
    "termination": { "mode": "mutual" }  // mutual|unilateral-with-notice|automatic
  }
}
```

- 契约修订 = 新 `revision` + 全体重签（对等网络里**没有单方修改权**，
  这是权威外化到契约的体现）；
- 废弃：`status→deprecated` 起算窗口，期满→`retired`；窗口内旧接口仍须应答
  （或回 `410 Gone` + 迁移指引）；
- 终止模式显式化：`mutual`（双方同意）/`unilateral-with-notice`（单方通知期）
  /`automatic`（到期自动）。

**去中心 vs 治理（v0.2 裁决）**：评审提出的"中央治理委员会"在邻居模型里
**不存在**——没有中央。治理体现在两处：(a) 双边契约的**全体签署**流程；
(b) **本地信任策略**对扩展/对端的态度。多方契约的治理靠契约文件本身
（`amend_requires` 声明），不靠外部机构。

## 6. 兼容性：机器判定，不采信声明

```jsonc
{
  "compat": {
    "supersedes": "sha256:<prev-hash>",
    "change_kind": "additive",              // 声明值（仅供人看，不可信）
    "verified_by": "tool:contract-diff-v1", // 机器判定结果（可信）
    "breaks": []                            // 若 breaking，列出破坏点
  }
}
```

**关键前瞻决策**：`change_kind` 是提交方写的、是 LLM 产出的——**不可信**
（又是信任根问题）。真正的兼容性由 `verified_by` 指向的工具**计算**：
对比新旧 schema，按确定性规则判定 additive/breaking/fix。声明与判定不符 →
拒绝落盘。这样"我说这是兼容的小改动"骗不过去。

（评审佐证：这是业界 schema registry / OpenAPI diff 的标准实践——
兼容性检查必须是**部署流水线门禁**，而非文档里的承诺。）

## 7. 组合与引用

- `$ref` **只允许本契约内部**引用（`#/types/...`）；
- 需要复用时**内联快照**（引用方复制内容 + 记来源 hash），而非跨契约动态解析；
- 理由：跨契约动态 `$ref` 引入加载顺序、网络依赖、循环引用——在"对方不在线"
  的邻居网络里是灾难。内联快照牺牲一点体积，换来确定性（§9 的前提）。

## 8. 符合性与测试（v0.2：升级为消费驱动）

```jsonc
{
  "conformance": {
    "tests": [
      { "name": "items-pagination",
        "ref": "test://server/tests/test_items.py::test_page",
        "hash": "sha256:...",        // 测试内容 hash，防签完换测试
        "provided_by": "instance-A" } // 谁提供该测试
    ]
  }
}
```

契约不仅声明接口，还**引用可执行的符合性测试**——把"我们说好了"升级为
"我们各自跑过同一组测试"。

**v0.2 增强（评审建议采纳）**：引入**消费驱动契约（consumer-driven contract）**
思想——不只是"提供方附测试"，而是**每个消费方声明自己依赖的最小字段集**，
提供方修订前必须跑通所有消费方声明的断言。这把"我改了没破坏你"从提供方的
自证，变成消费方的验收。这层是 P2 端到端验收的抓手，现在留位置。

## 9. 规范化与完整性（hash 共识的物理前提）

```jsonc
{
  "integrity": {
    "canon": "RFC8785-JCS",        // 规范化：JSON Canonicalization Scheme
    "hash": "sha256:<canonical-bytes>",
    "sigs": [ { "party": "instance-A", "alg": "ed25519", "sig": "..." } ]
  }
}
```

**这一节不写清楚，hash 共识根本跑不起来**：双方 JSON 序列化只要在键序、
空白、数字格式上有一点不同，hash 就永远不等，谈成的契约永远对不上。

规则：
- hash 只对**规范形式**（RFC 8785 JCS）计算，`integrity` 字段自身不参与——
  JCS 给出确定性属性排序 + 严格数值表示（治 `0.1` vs `0.1000...` 精度歧义）；
- 时间一律 RFC3339 UTC，数字规范化表示，字符串 NFC 归一化；
- **default/nullable 必须显式（v0.2 补充）**：JCS 只能规范化"存在的数据"，
  治不了"字段该不该存在"——一个 optional 字段缺失时，消费方可能各自
  理解为 `0`/`""`/`null`/不存在，业务逻辑随即分叉。因此契约里每个可选字段
  必须**显式声明**默认值（且提供方序列化时须包含）或明确 `nullable` 含义；
- 规范化规则本身可版本化（`norm` / `canon` 引用规则版本），换算法 =
  一次显式的 `contract_version` 升级；
- 签名覆盖 canonical 字节，机制对齐 JWS/JCS 的密码学绑定——防"签完换内容"。

## 10. 校验器注册表（防耦合）

`contract_version` → 校验器的注册表，插件式：

```
validator_registry = {
  "1.0": ContractValidatorV1,      # 核心校验（结构、作用域、命名空间）
  "1.0/http": HttpKindValidatorV1, # kind 子校验
  ...
}
```

- 解析器据 `contract_version` 选校验器，**校验器与 schema 版本绑定而非与代码库绑定**；
- 新增 kind / 新规范版本 = 注册新校验器，不修改既有校验器（开闭原则）；
- 这层与 Zeta 的 codec 层同构（都是插件注册表）——**同一套扩展哲学**。

## 11. 完整示例（items-api）

见 `zeta_邻居领域模型.md` §4 的协商时序；完整 JSON 见附录或 P1 实现时的
`examples/items-api.contract.json`（本轮从略——结构同 §1，字段齐备）。

对应 wire（A2A 侧）：`propose(contract 全文, hash=X)` → B 校验：结构✓、
作用域规范化✓、`change_kind` 与机器判定一致✓、hash 与 canonical payload 一致✓、
安全区无未知字段✓ → `ack(hash=X)` → 双方签名落交换区。
**任一步校验失败 = 不 AGREED。**

## 12. 待决问题（留给下一轮/评审）

1. `types` 是否允许静态导入外部标准（如 OpenAPI 片段）？倾向：允许带 hash 的
   `x-import` 静态导入，仍不动态解析（守 §7）；
2. 多方契约的部分签署态（2-of-3 已签待第三方）如何表达？
3. `conformance.tests` 的引用协议（`test://`）解析规则——P2 再定；
4. 安全区具体清单需逐字段裁定（§2.3 现在只给了三类）；
5. expand-contract 的"迁移进度"如何机器可查（§2.1）——需要设计 `deprecations[]` 结构。 → **已解决（§2.1.1）**。

---

## 附：v0.1 → v0.2 变更摘要（评审对撞记录）

**设计评审路（deepseek）采纳：**
- `negotiated` 顶层字段（profile 协商产物落进契约，与领域模型 §3 呼应）；
- `resources.default_effect: deny` 显式化（默认拒绝，比"列允许"抗遗漏）；
- scope `target.type` 开放枚举（file/dir/s3/http/queue/function/custom）；
- `lifecycle.termination.mode` 显式化；签名绑定 `canonicalization: RFC8785`；
- **扩展键名改用 URI 前缀**（原 `x-vendor` → `urn:`/`https://`），全局无冲突。

**演进红队路（glm）采纳：**
- **expand-contract 收缩协议（§2.1）**——修正 v0.1 的过度谨慎：
  "append-only 永不删"本身是债（契约臃肿、不敢演进），需要安全的删除轨道；
- **default/nullable 显式（§9）**——JCS 治不了"字段存不存在"，这是 v0.1 的洞；
- **消费驱动契约测试（§8 增强）**——从"提供方附测试"到"消费方声明依赖"；
- **组合子 schema 禁用 `additionalProperties:false`（§3）**——防空 schema；
- 兼容性检查作为流水线门禁（佐证 §6 的机器判定方向）。

**我裁决拒绝的：**
- parties 限死为 2（设计评审的 `maxItems:2`）→ 坚持数组 ≥2（前瞻性）；
- "中央治理委员会"→ 邻居模型无中央，治理落在全体签署 + 本地策略（§5）。
