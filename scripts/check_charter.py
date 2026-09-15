#!/usr/bin/env python3
"""宪章守卫 —— 防止 docs/CHARTER.md 被静默修改。

docs/CHARTER.md 是 FP 的判据（见该文件《立宪的由来》）。
宪章第 0 条承诺：改变判据只允许通过一次**显式的、可审计的修宪行为**，
禁止以重构 / 重命名 / 注释 / 渐进削弱等方式隐式改变其约束力。

本脚本把该承诺机械化（宪章第 1 条：可检测，而非可批准）：

  1) 宪章哈希 == ANCHOR                     → 通过（未被改动）
  2) 哈希不符，但已显式声明修宪              → 通过，并提示更新 ANCHOR
     （环境变量 FP_CHARTER_ALLOW=1，对应一次公开的 `charter:amend` 提交）
  3) 哈希不符且未声明修宪                    → 阻断
  4) 宪章文件缺失                            → 阻断（判据不可被静默删除）

用法：
    python scripts/check_charter.py                          # 手动检查
    FP_CHARTER_ALLOW=1 git commit -m "charter:amend — <理由>"  # 显式修宪

退出码：0=通过 / 1=阻断
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import sys

# 立宪锚点：docs/CHARTER.md 在 commit d50cc53 的 sha256。
# 修宪时必须同步更新此常量——这是"改判据很昂贵"的具体来源。
ANCHOR = "ddf4d95aee64b16b9f94f1d139f78632ea3c74663911e04118d337c4a9d1ed9b"
CHARTER = pathlib.Path("docs/CHARTER.md")
ALLOW_ENV = "FP_CHARTER_ALLOW"


def sha256_of(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if not CHARTER.exists():
        print("✗ 宪章守卫：docs/CHARTER.md 不存在。")
        print("  判据不可被静默删除。若确要废止或迁移，请显式修宪：")
        print(f"  {ALLOW_ENV}=1 git commit -m 'charter:amend — 废止/迁移判据'")
        return 1

    current = sha256_of(CHARTER)
    if current == ANCHOR:
        print("✓ 宪章守卫：docs/CHARTER.md 与锚点一致，未被改动。")
        return 0

    if os.environ.get(ALLOW_ENV) == "1":
        print(f"⚠ 宪章守卫：检测到宪章已改动，但已显式声明修宪（{ALLOW_ENV}=1）。")
        print(f"  旧锚点: {ANCHOR}")
        print(f"  新哈希: {current}")
        print("  请将本脚本 ANCHOR 更新为新哈希，并让提交信息含 'charter:amend'。")
        return 0

    print("✗ 宪章守卫：docs/CHARTER.md 与锚点不一致，且未声明修宪。")
    print(f"  锚点  : {ANCHOR}")
    print(f"  当前  : {current}")
    print("  判据不得被静默修改。若确为有意修宪，请显式放行：")
    print(f"  {ALLOW_ENV}=1 git commit -m 'charter:amend — <理由>'")
    return 1


if __name__ == "__main__":
    sys.exit(main())
