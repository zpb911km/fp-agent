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
| 试点1 | fp-core/core/conversation.py | 166 | ? | ? | ? | ? | 流水线首测 |

## 待办

- [ ] 试点1：conversation.py（子 agent 修复 → 主验证 → 提交）
- [ ] 试点2：option.py
- [ ] 试点数据汇报 → 用户决定铺开方式
- [ ] 环境类问题清单（py.typed、第三方无类型）→ 用户拍板

## 遇到的坑

（待填）
