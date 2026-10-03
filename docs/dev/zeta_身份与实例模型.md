# Zeta 身份与实例模型（重构方案 · 草案 v0.4）

> 状态：**已定案并实现（v0.5）**。本文件提出对 `zeta_邻居领域模型.md` §1.4 / §1.5
> 的修订——**实例/身份模型**。协议面（Envelope / Profile / Contract）不受影响。
> 实现落点：`discovery.py`（`InstanceRegistry` / `derive_instance_name` /
> `check_session_workspace`）、`plugin.py`（sid 接线 + 错配提示）、`domain.py`
> （`workspace` 字段 + 路由键）。测试全绿（679 passed）。

## 0. 为什么改

原设计把「一个路径 ↔ 一个邻居」写死（一目录一实例，cwd 即身份键），把一个**位置**
误当成**身份**，代价在「同一项目并行多个 agent」时集中爆发：

| 症状 | 真实体验 |
|---|---|
| 并行受限 | 同一目录开不了第二个实例，被迫换目录 |
| 记忆不共享 | 换了目录 → `.fp/memory` 分叉 → 同项目记忆割裂 |
| 路径指代不明 | 路由键是 cwd，路径相近/同名时谁是谁分不清 |
| 名称不稳 | `name = basename(cwd)+pid`，reload 换名、旧卡变僵尸 |

## 1. 根因：位置 / 实例 / 身份 三位一体被强绑

```
workspace ≡ instance ≡ identity ≡ cwd          （现行，1:1:1:1）
```

四个概念本应正交：

| 概念 | 本义 | 应然关系 |
|---|---|---|
| **workspace** | 记忆/任务的归属边界（`.fp/` 所在） | 1 : N（可挂多实例） |
| **instance** | 一次运行的进程 | N : 1 |
| **identity** | 我是谁、如何被寻址 | 独立、稳定、唯一 |
| **cwd** | 进程当前所在目录 | 只是实例的一个**属性** |

### 关键洞察 A：记忆分叉不是记忆机制的问题

`.fp/memory/`、`.fp/tasks.json` **本来就是 workspace 级、同目录共享的**（已核实）。
**记忆割裂 = 单实例锁逼出来的**：锁不让同目录开第二个实例 → 人换目录 → `.fp/` 分叉。
撤掉那道锁就解决 —— **一行记忆代码都不用改**。

### 关键洞察 B：人格就是上下文，锚点是 sid

不需要给实例"额外起名"。**一个"人格"= 一段长期上下文 = 一个会话（sid）**。
sid 全局唯一、reload/resume 稳定，天然是身份的锚点；"召回一个人" = `fp -r <sid>`。

## 2. 目标与不变量

**目标**
1. 同一 workspace 可并行 N 个实例；
2. 同 workspace 实例**共享**记忆与任务（天然，因为都是 workspace 级）；
3. 身份**稳定**（reload 不变）、**唯一**（全局）、**可召回**（resume）；
4. 路由**明确**（精确名 > 能力 > 职能 > 同项目）。

**不变量**
- 身份是**本地策略**，不进 wire —— codec/transport 不感知身份如何生成。
  验证：删掉 a2a codec，身份相关测试仍全绿。
- 权限红线不变：实例只写自己的名片与收件箱（§1.2 自写隔离）。
- 「一目录一实例」的**能力**保留为可选策略（`--single`），不再默认。

## 3. 方案：三分解耦

### 3.1 workspace 与 instance 松绑（1 : N）—— 解决记忆共享

`.fp/` 下单个锁文件 → **每实例一个登记**：

```
<workspace>/.fp/zeta/instances/<name>.json   # {name, sid, pid, workspace, cwd, started_at}
```

- 启动**不再拒绝**第二实例；各写自己的登记（写隔离不破）；
- 启动时扫描同目录其他登记，`pid` 已死的 → 回收（僵尸清理）；
- `--single`：保留旧语义（有活跃实例则本实例降级不启用邻居面，行为不变）；
- 退出删自己的登记。

**副产品**：同 workspace 多实例 → `.fp/memory`、`.fp/tasks.json` **自动共享**。

### 3.2 身份锚点 = sid（人格即上下文）

**唯一锚点：会话 sid。** 不再有 `--as` 命名层、不用 pid、不用 workspace-hash。

```
name := <workspace名>-<sid 短handle>      # 唯一、reload 稳定、不可读无所谓
```

- **不可读不是问题**：name 是**机器 handle**（寻址用），可读性由 `business`/`tags`/
  `workspace` 承担 —— 筛选看这些，选定后用 name 精确点对点；
- **sid 短 handle**：取 sid 的 hash 短形式（**不暴露原 sid** 给邻居，避免泄露本地会话标识）；
- **`--as` 降级为可选显示昵称**（写进 business），永远不是身份锚点。

**生命周期 → 身份映射**（已查证）：

| 操作 | sid | 身份 |
|---|---|---|
| reload（热重启） | 不变（`FP_RELOAD_SID`） | **不变** ✅ |
| `fork_new` / `switch_to` | 变 | 变 / 切换（新人格 / 召回旧人格） |
| `clear` | 不变（只清内容） | 不变（失忆） |
| 进程退出 | 留住磁盘 | 可 `resume` 召回 |

**两种寻址统一于 sid**：
- 人类召回 → `fp resume`（`commands/resume.py` 列会话）→ 选中 → 人格回来；
- 机器寻址 → `peer_list` 拿 name → `peer_send`。

### 3.3 路由键升级

`NeighborCard` 增 `workspace` 字段；路由优先级：

```
name（精确点对点）> tags（能力）> business（职能）> workspace（同项目）
```

「同项目」判据明确为 **workspace 相等**，而非 cwd 字符串相近。

## 4. 开放问题

### 4.1 workspace 的边界是什么
「同项目」指：(a) 目录（现状语义）｜ (b) 显式 `fp --workspace` ｜ (c) 向上找 `.git/` 根
（monorepo 的 `frontend/` 与 `backend/` 共享 workspace）。

### 4.2 resume 跨 workspace：人格与位置错配（sid 解耦后新冒出的）
sid 全局，`.fp/memory` 是 workspace 级 —— `/projA` 的会话 X 在 `/projB` 里 `fp -r X`：
**人格来自 A，记忆读 B**。规则候选：
- (i) workspace 跟会话走（会话 meta 存 workspace）→ 一致，但跨目录 resume 会读原目录记忆；
- (ii) workspace 跟启动目录走 → 简单，错配时**显式提示一次**。

**推荐 (ii)+提示**：记忆是"这片代码库的知识"，跟着代码库走，不被对话拖走。

## 5. 迁移与爆炸半径

| 文件 | 改动 |
|---|---|
| `discovery.py` | `InstanceLock` → `InstanceRegistry`（~60 行）；`build_card` 加 `workspace` |
| `plugin.py` | name 生成改 sid 派生 + 登记获取（~15 行） |
| `domain.py` | `NeighborCard` 加 `workspace` 字段（序列化 ~6 行） |
| `test_zeta_discovery.py` | 锁测试改写为多实例登记测试 |
| `zeta_邻居领域模型.md` | §1.5 重写、§1.4 路由键更新 |
| **不动** | `codec` / `transport` / `contracts` / `peer`（不感知身份） |

## 6. 决策（已定案）

- **A 身份锚点**：**sid 单锚点**（人格 = 上下文 = 会话；无 `--as`）
- **B 多实例默认**：**B1** 默认允许多实例；`FP_ZETA_SINGLE=1` 才独占
- **C workspace 边界**：**C(a)** workspace = 启动目录（留 (b)/(c) 扩展位）
- **E resume 跨 workspace**：**E(ii)** workspace 跟启动目录 + 首次错配提示

**实现状态**：已落地，全量测试 679 passed / 2 skipped，pyright 0 errors，ruff 全过。

## 7. 对现有文档的修订锚点

- §1.4「名片即路由表」：路由依据 `cwd` → `workspace`；名片示例补 `workspace`；
- §1.5「一目录一实例」：**标题与结论整体作废**，替换为本文 §3.1 的 1:N 登记模型；
  保留其中「降级可见」（不启用邻居面须告知 LLM）原则，套用到 `--single` 冲突场景。
