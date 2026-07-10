---
name: qwen_vision_plugin
description: qwen_vision 插件已注册到 tools 系统
type: project
created: 2026-07-11 01:16
---

## qwen_vision 插件

路径：`/media/zpb/data/codes/AI/agent/tools/extensions/qwen_vision_plugin.py`

一个工具插件，提供图像识别能力。底层调用 `qwen_vision.py` 模块（在 `llm_api_hack/` 目录下），通过 Qwen Web API + Playwright 浏览器驱动实现。

### 调用方式
Agent 自动识别，用户或 skill 中直接写：
```python
# 在 conversation 中模型会自动调用
qwen_vision(image_path="/tmp/screenshot.png", query="描述这张图片")
```

### 参数
- `image_path`: 图像文件绝对路径（必须）
- `query`: 询问文本（可选，默认"描述这张图片"）

### Cookie 配置
cookie 保存在 `~/.qwen_cookie`，格式：
```
cna=xxx; aui=xxx; token=xxx; ...
```
也支持环境变量 `QWEN_COOKIE`。

### 依赖
- `pip install oss2 playwright`
- `playwright install chromium`
