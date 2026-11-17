"""仓库根 pytest conftest — 全局测试环境引导。

必须在任何测试模块 import fp_core.config **之前**设置 LLM 三键：
config 在模块导入时把 `_value(...)` 绑定为模块级常量，全新机器 / CI runner
没有 ~/.config/fp/config.json，若此刻环境变量也未设，常量定格为空串 →
Agent 构造时 check_llm_config() 拒绝 → 70 个用例连环挂。
测试文件顶部的 os.environ.setdefault 救不了（conftest 与更早收集的测试
模块先一步 import 了 config），只能在根 conftest 引导。

注：产品侧已「零默认」（api_key/base_url/model 均无内置兜底，缺失即硬拒启动），
此处是纯测试脚手架兜底，与产品语义无关。

优先级 JSON > 环境变量，故开发者本机 config.json 照常生效，此默认值只兜底。
"""

import atexit
import importlib.util as _ilu  # 下方「可选依赖缺位」段用它探测 playwright（import 必须在顶部：E402）
import os
import shutil
import tempfile
from collections.abc import Generator
from unittest.mock import patch

import pytest

os.environ.setdefault("LLM_API_KEY", "sk-test-key-for-pytest")
os.environ.setdefault("LLM_API_BASE_URL", "https://api.deepseek.com/v1")
os.environ.setdefault("LLM_MODEL", "deepseek-chat")

# git 身份兜底：全新 runner / 无 ~/.gitconfig 的机器上，fp ext 的自动 commit
# 会以「作者身份未知」失败（test_ext 17 个用例连环挂）。仅 setdefault——
# 用户已配置的真实身份优先。env 影响 commit 的 author/committer 两级，
# 与 test_check_docs_sync_cli.py 既有约定一致。
os.environ.setdefault("GIT_AUTHOR_NAME", "fp-tests")
os.environ.setdefault("GIT_AUTHOR_EMAIL", "fp-tests@local")
os.environ.setdefault("GIT_COMMITTER_NAME", "fp-tests")
os.environ.setdefault("GIT_COMMITTER_EMAIL", "fp-tests@local")


# ══════════════════════════════════════════════════════════════
# 用户目录隔离：在这里**钉死**，别让「谁先 import」决定数据目录
# ══════════════════════════════════════════════════════════════
# 背景（2026-11-17 实测）：三个测试模块（fp-core/tests/test_config.py、
# test_llm_providers.py、fp/tests/test_setup.py）在**模块 import 期**执行
# `os.environ.setdefault("XDG_DATA_HOME", /tmp/fp_test_data)`，本意是「在
# import config 之前隔离用户目录」。但 pytest 在**收集阶段**就 import 所有测试
# 模块（远早于任何 fixture 运行）→ 整个 session 的 os.environ 被永久改写：
# 这不是 monkeypatch，不会回滚，`platform_utils.get_data_dir()` 从此全进程
# 返回 /tmp/fp_test_data/fp。两处真实伤害：
#   1. 那个路径是**跨 run 共享**的固定常量 → 上一次跑的残留（token、会话、
#      资产）会喂给下一次，红/绿取决于历史，不取决于代码。
#   2. e2e 的 `server` fixture 起/复用一个 WebUI **子进程**后要读它的 token，
#      而 token 落在服务器真正用的数据目录里；一旦两边理解不一致
#      （复用人工起的 8899 服务时必然不一致）→ 401「Token 无效」或
#      FileNotFoundError，且只在全量跑时复现、分包单跑全绿。
# 根 conftest 的 import 早于一切测试模块，在这里钉死即让那三处 setdefault
# 退化为 no-op。它们**保留**不动：`pytest packages/fp-core/tests` 时
# rootdir=packages/fp-core，pytest 只从 rootdir 向下加载 conftest，本文件
# 不参与，那时仍需模块级兜底（同 §_isolate_* 的 rootdir 边界说明）。
# 每次 run 一个**全新**临时目录：跨 run 不串味。
# 钉住之前先把「这台机器真实的数据根」记下来，导出给测试：少数用例的语义本
# 就是「本机装了什么就测什么」（fp-core/tests/test_subagent_asset.py 动态加载
# 已安装的 subagent 资产），隔离不该顺手把它们全变成「没装」而静默跳过。
try:
    from fp_core.platform_utils import get_data_dir as _real_get_data_dir

    os.environ["FP_TEST_REAL_DATA_DIR"] = _real_get_data_dir()
except Exception:  # 拿不到就不给旁证：那几个用例按「未安装」跳过，不影响其余
    pass

_TEST_ENV_DIR = tempfile.mkdtemp(prefix="fp_pytest_env_")
for _var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA"):
    os.environ[_var] = _TEST_ENV_DIR
atexit.register(shutil.rmtree, _TEST_ENV_DIR, True)  # ignore_errors：干净退出即可，清不掉不算失败


# ══════════════════════════════════════════════════════════════
# 可选依赖缺位时整目录不收集（e2e 需 playwright）
# ══════════════════════════════════════════════════════════════
# playwright 既不在各包 pyproject，也不在 docs/dev/测试.md 的测试依赖清单里
# （本机是手装的）→ CI / 未装它的机器上，e2e/conftest.py 顶层的
# `from playwright.sync_api import expect` 在**收集期**就 ImportError，
# 整个全量 pytest 直接 exit 2（2026-11-13 实测：5 个测试 job 连环挂）。
# 未装则整目录不收集：装了的本机照常跑 e2e，没装的环境静默跳过而非崩。
# 注：`-m "not e2e"` 救不了——标记过滤发生在**导入之后**，模块顶层 import 已经炸；
# `--ignore=...` 同为收集期忽略、能救，但要人手写每条命令，易漏 → 用 find_spec 自动判。
# （`_ilu` 的 import 在文件顶部：本文件的引导语句都在 import 之后执行，import 放这里会 E402）
if _ilu.find_spec("playwright") is None:
    collect_ignore = ["packages/fp-webui/tests/e2e"]


# ══════════════════════════════════════════════════════════════
# 全局测试隔离（#219 上提：原 fp-core/tests/conftest.py 同款实现，单点维护）
# ══════════════════════════════════════════════════════════════
# 为什么在根 conftest：rootdir=仓库根时（`pytest` / `pytest packages` /
# `pytest packages/fp-webui/tests`）根 conftest 对**所有包**生效，一处定义
# 全包继承，杜绝 fp-core / fp-webui 各留一份同款 fixture 的双份维护。
#
# ⚠️ 已知边界（实测，2026-11-09）：`pytest packages/fp-core/tests`（含
# pre-commit 门禁命令）的 rootdir=packages/fp-core（该包 pyproject 带
# [tool.pytest.ini_options]），pytest 只从 rootdir 向下收集 conftest →
# **根 conftest 不加载**。故 fp-core/tests/conftest.py 保留一段按路径再导出
# （加载本文件的 fixture 对象，非复制实现），fp-webui 无此问题（其 pyproject
# 无 pytest 段，rootdir 恒为仓库根）故其 conftest 已删除。
@pytest.fixture(autouse=True)
def _isolate_sessions_dir(tmp_path) -> Generator[None, None, None]:
    """全局隔离会话目录：任何测试都不允许写真实用户会话数据。

    背景（#219）：Agent.__init__ 无条件创建 SessionManager 并写入
    config.SESSIONS_DIR（~/.local/share/fp/sessions/）。fp-webui/tests 曾无
    conftest，test_webui_bridge_turn.py 跑真实 Agent.process("hello")，
    5 次 × 4 用例 = 20 条 summary=hello 垃圾会话直落生产目录。
    更早 fp-core 的 test_agent_tool_parallel.py 也污染过一次（sleep_1 等）。

    同时 patch 两个引用点：
      - fp_core.config.SESSIONS_DIR        （agent.py 里 os.makedirs 用）
      - fp_core.core.session.SESSIONS_DIR  （session.py:22 导入时绑定，落盘用）
    """
    import fp_core.config as cfg
    import fp_core.core.session as session_mod

    tmp = str(tmp_path / "sessions")
    with (
        patch.object(cfg, "SESSIONS_DIR", tmp),
        patch.object(session_mod, "SESSIONS_DIR", tmp),
    ):
        yield


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path) -> Generator[None, None, None]:
    """全局隔离 FP 数据根：测试不扫描/不写真实 fetched/public/private 资产。

    背景：ToolRegistry / Agent 构造时会扫 user_dirs("tools"/"plugins")，即
    {DATA}/{fetched,public,private}/...。若不隔离，用户安装的 private 插件
    会污染断言（被删除/迁移的旧插件等）。把 config._FP_DATA_DIR 指向 tmp，
    user_dirs() 全部落到临时目录。
    """
    import fp_core.config as cfg

    tmp = str(tmp_path / "fp_data")
    with patch.object(cfg, "_FP_DATA_DIR", tmp):
        yield
