---
name: fp_core_import_refactor
description: fp_core 包 import 路径重构完成：旧单包路径 → fp_core.xxx 绝对导入
type: project
created: 2026-07-11 01:16
---

已完成 fp_core 包内所有 Python 文件的 import 路径重构：

1. 全局替换：import config/display → from fp_core import config/display
2. 核心模块：from core.xxx → from fp_core.core.xxx
3. 其他模块：from commands/tools/extensions/skills/prompts → from fp_core.xxx
4. 删除 sys.path.insert hack（session.py, commands/__init__.py）
5. config.py: CONFIG_JSON → config.default.json（包内路径）
6. session.py: SESSIONS_DIR → ~/.local/share/fp/sessions
7. prompts/agent.py: 简化路径计算
8. 更新 importlib.import_module 调用中的 package 名称
9. 修复所有懒导入（缩进中的残留旧式 import）

验证：全部 18 个模块导入成功，0 残留旧式 import。
