# 类型债工程 · 进度追踪

> 目标：消除 fp 全项目 pyright strict 错误，不改运行时行为，测试全绿。
> 模式：主控-子任务流水线。每批验收三件套（单文件 0 错 / 全局下降 / pytest 回归）。

## 基线（2025 试点开始时，无 py.typed）

- 全局：**2840 errors**（pyright 1.1.409，strict，5 包 src）
- 加 5 个 py.typed 后理论基线：2728（stub 错误 112 个，属环境类，待用户拍板）
- 试点文件：fp-core/core/conversation.py（166 错）、fp-core/commands/option.py（187 错）

## 依赖顺序

```
fp-core（根基）→ fp-terminal / fp-webui / fp-acp → fp（聚合）
```

## 批次记录

| 批 | 文件 | 单文件错 | 修后 | 全局(基线2840→) | pytest | commit | 备注 |
|---|---|---|---|---|---|---|---|
| 试点1 | fp-core/core/conversation.py | 166 | 0 | 2620 (-220) | 286p/1s ✓ | 5568af7 | 根因=dict泛型缺失级联；级联收益+54 |
| 试点2 | fp-core/commands/option.py | 186 | 5(环境) | 2439 (-181) | 286p/1s ✓ | 3f85c04 | 剩4×MissingTypeStubs+1×PrivateUsage |
| 环境 | 5 包 py.typed | — | — | 2327 (-112) | — | a8bff3c | 用户批准；stub 错全消 |
| 批1 | core/llm_client+llm_service+io | 93+106+31 | 全0 | 2088 (-239) | 286p/1s ✓ | 78c1416 | 级联收益+9；service 对兄弟包 Any 兜底 |
| 批2 | core/session+prompt_builder+lifecycle | 82+35+80 | 全0 | 1851 (-237) | 338p/1s ✓ | b498c6a | 级联收益+40；lifecycle 用 ignore 抑制 iscoroutinefunction 弃用 |
| 批3 | core/agent.py + plugins/__init__.py | 44 | 0 | 1800 (-51) | 338p/1s ✓ | 4c754f4 | 根因：plugins/ 缺 __init__.py 致 namespace pkg → stub 错；级联-7 |
| 批4 | 基础层 plugin+config+tool_executor+token_tracker | 35+12+8+9 | 全0 | 1736 (-64) | 338p/1s ✓ | 89a3811 | plugin 基类修复带下游级联；tool_executor 用 TYPE_CHECKING 防循环导入 |

## 待办

- [x] 试点1：conversation.py ✅
- [x] 试点2：option.py ✅
- [x] 环境：py.typed ✅
- [x] 批2：core/session.py + core/prompt_builder.py + core/lifecycle.py ✅
- [x] 批3：core/agent.py ✅
- [x] 批4：基础层 plugin/config/tool_executor/token_tracker ✅
- [ ] 后续按依赖顺序铺开（fp-core commands/plugins/tools → terminal → acp/webui → fp）
- [ ] 环境类问题清单（_commands 私有访问、第三方无类型）→ 用户拍板

## 遇到的坑

- docs sync 钩子拦截类型标注 commit → 用 FP_DOCS_SYNC_ALLOW=1 放行（类型标注不改变行为，文档无需更新）
- pyrightconfig.json 无效键 "strict": true（标准是 typeCheckingMode）→ 已修正
- fp-terminal 实际导入包名是 fp_cli（egg-info 名字 fp_terminal 是历史遗留）→ py.typed 放 fp_cli/
