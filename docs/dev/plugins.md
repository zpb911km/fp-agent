# 插件目录约定

## 文件即开关

本目录下的每个 `.py` 文件对应一个独立插件。

**启用/停用不需要改配置、不碰代码——改文件名就够了。**

```
plugins/
├── notification.py         # ✅ 启用
├── weather.py              # ✅ 启用
├── something.py.disabled   # ❌ 停用（加了后缀 .disabled）
├── old_idea.py.backup      # ❌ 停用（加了后缀 .backup）
├── __pycache__/            # ⚠️ Python 缓存，自动忽略
└── base/                   # ⚠️ 插件基类，不直接加载
```

### 规则

| 文件名模式 | 行为 |
|-----------|------|
| `name.py` | ✅ 加载（有效插件） |
| `name.py.任意后缀` | ❌ 跳过（停用标记） |
| `_name.py` | ❌ 跳过（内部模块） |
| `base/`, `__pycache__/` | ❌ 自动忽略 |

### 使用场景

```
# 临时停用一个插件
mv notification.py notification.py.disabled

# 恢复
mv notification.py.disabled notification.py

# 保留一份旧版本参考
mv old_plugin.py old_plugin.py.v2
```

### 为什么这样设计

1. **零配置** — 文件系统本身就是配置层
2. **无侵入** — 不改代码、不写配置文件、不碰注册表
3. **原子操作** — `mv` 命令即开关，不会出现"改了配置但忘重启"的状态不一致
4. **版本兼容** — 停用的插件原样留在目录里，随时可恢复，不会丢失历史

---

## 如何编写一个插件

参见 `plugins/base/plugin.py` 中的 `Plugin` 基类。

快速开始：

```python
from fp_core.plugins.base.plugin import Plugin, PluginConfig, PluginRegistry
from fp_core.core.lifecycle import LifecycleHook

class MyPlugin(Plugin):
    name = "my_plugin"
    version = "1.0.0"

    def on_register(self, lifecycle):
        lifecycle.register(
            LifecycleHook.ON_MESSAGE_RECEIVED,
            self.on_message,
            priority=50,
            name=f"my_{LifecycleHook.ON_MESSAGE_RECEIVED.name}",
        )

    async def on_message(self, ctx, **kwargs):
        print(f"收到消息: {kwargs.get('content')}")
```

注册到 Agent：

```python
agent.plugins.register(MyPlugin())
```
