---
name: "plugin_management"
title: "插件管理 — 使能/失能"
description: "生命周期插件 (plugins/) 和工具插件 (tools/plugins/) 的启用/禁用机制：文件即开关，通过重命名控制加载状态。"
category: "core"
version: "1.0"
priority: 80
---

# 插件管理 — 使能/失能

## 核心原则：文件即开关（File-as-Switch）

插件通过**文件命名约定**控制是否被加载，无需修改任何代码或配置文件。

## 插件类型与加载目录

| 类型 | 目录 | 加载器 |
|------|------|--------|
| **生命周期插件** | `{data_dir}/plugins/` | `PluginRegistry.scan()` |
| **工具插件** | `{data_dir}/tools/plugins/` | `ToolRegistry._load_from_dir()` |

其中 `{data_dir}` 跨平台：
- Linux: `~/.local/share/fp/`
- Windows: `%LOCALAPPDATA%/fp/`

## 使能/失能规则

### 生命周期插件 (`plugins/`)

加载器使用 `fname.endswith(".py")` 过滤，目录扫描逻辑：

| 文件名 | 结果 | 说明 |
|--------|------|------|
| `name.py` | ✅ **加载** | 标准命名，正常运行 |
| `name.py.disabled` | ❌ **跳过** | `".py.disabled"` 不以 `.py` 结尾，被过滤 |
| `_name.py` | ❌ **跳过** | 以下划线开头，视为内部模块 |
| `base.py`, `setup.py` | ❌ **跳过** | 硬编码黑名单 `SKIP_FILES` |

### 工具插件 (`tools/plugins/`)

加载器使用 `fname.endswith("_plugin.py")` 过滤：

| 文件名 | 结果 | 说明 |
|--------|------|------|
| `xxx_plugin.py` | ✅ **加载** | 标准命名 |
| `xxx_plugin.py.disabled` | ❌ **跳过** | `"_plugin.py.disabled"` 不以 `_plugin.py` 结尾 |
| `_xxx_plugin.py` | ⚠️ **加载** | **注意**：工具插件加载器**没有**下划线跳过逻辑 |

## 操作命令

### 禁用插件

```bash
# 生命周期插件
mv {data_dir}/plugins/xxx.py {data_dir}/plugins/xxx.py.disabled

# 工具插件
mv {data_dir}/tools/plugins/xxx_plugin.py {data_dir}/tools/plugins/xxx_plugin.py.disabled
```

### 启用插件

```bash
# 生命周期插件
mv {data_dir}/plugins/xxx.py.disabled {data_dir}/plugins/xxx.py

# 工具插件
mv {data_dir}/tools/plugins/xxx_plugin.py.disabled {data_dir}/tools/plugins/xxx_plugin.py
```

### 查看当前插件清单

```bash
# 查看已加载的（.py 结尾）
ls {data_dir}/plugins/*.py 2>/dev/null
ls {data_dir}/tools/plugins/*_plugin.py 2>/dev/null

# 查看已禁用的（.disabled 结尾）
ls {data_dir}/plugins/*.disabled 2>/dev/null
ls {data_dir}/tools/plugins/*.disabled 2>/dev/null
```

## 注意事项

1. **生效时机**：插件加载发生在启动时的 `scan()` 调用中。修改文件名后需**重启 Agent** 才能生效。
2. **用户覆盖**：同名插件，用户目录下的版本会覆盖内置版本（`PluginRegistry` 和 `ToolRegistry` 均支持）。
3. **工具插件无下划线跳过**：生命周期插件跳过 `_name.py`，但工具插件没有此逻辑，请知悉。
4. **安全性**：此机制只控制加载与否。已运行的插件可通过 `Plugin.disable()` 运行时禁用，但钩子仍在内存中。
