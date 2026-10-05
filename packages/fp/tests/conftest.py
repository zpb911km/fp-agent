"""fp 包测试环境引导 — git 身份兜底。

fp ext 的 new/remove/promote 等操作会自动 git commit，全新机器 / CI runner
没有 user.name/user.email 时 commit 以「作者身份未知」失败，test_ext 连环挂。
单包跑（rootdir=packages/fp）加载不到仓库根 conftest，故在此再兜一层。
仅 setdefault——用户已配置的真实身份优先。
"""

import os

os.environ.setdefault("GIT_AUTHOR_NAME", "fp-tests")
os.environ.setdefault("GIT_AUTHOR_EMAIL", "fp-tests@local")
os.environ.setdefault("GIT_COMMITTER_NAME", "fp-tests")
os.environ.setdefault("GIT_COMMITTER_EMAIL", "fp-tests@local")
