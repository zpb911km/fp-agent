"""仓库根 pytest conftest — 全局测试环境引导。

必须在任何测试模块 import fp_core.config **之前**设置 LLM_API_KEY：
config 在模块导入时把 `_value("LLM_API_KEY", "")` 绑定为模块级常量，
全新机器 / CI runner 没有 ~/.config/fp/config.json，若此刻环境变量也未设，
常量定格为空串 → Agent 构造时 check_llm_config() 拒绝 → 70 个用例连环挂。
测试文件顶部的 os.environ.setdefault 救不了（conftest 与更早收集的测试
模块先一步 import 了 config），只能在根 conftest 引导。

优先级 JSON > 环境变量，故开发者本机 config.json 照常生效，此默认值只兜底。
"""

import os

os.environ.setdefault("LLM_API_KEY", "sk-test-key-for-pytest")

# git 身份兜底：全新 runner / 无 ~/.gitconfig 的机器上，fp ext 的自动 commit
# 会以「作者身份未知」失败（test_ext 17 个用例连环挂）。仅 setdefault——
# 用户已配置的真实身份优先。env 影响 commit 的 author/committer 两级，
# 与 test_check_docs_sync_cli.py 既有约定一致。
os.environ.setdefault("GIT_AUTHOR_NAME", "fp-tests")
os.environ.setdefault("GIT_AUTHOR_EMAIL", "fp-tests@local")
os.environ.setdefault("GIT_COMMITTER_NAME", "fp-tests")
os.environ.setdefault("GIT_COMMITTER_EMAIL", "fp-tests@local")
