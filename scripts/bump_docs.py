#!/usr/bin/env python3
"""
bump_docs.py — 五块卵石文档版本同步工具

用法:
    python scripts/bump_docs.py 0.2.0              # 默认使用当天日期
    python scripts/bump_docs.py 0.2.0 2026-07-01   # 指定日期

功能:
    1. 在 CHANGELOG.md 顶部插入新版本条目
    2. 更新 CHANGELOG.md 底部的发布历史表格
    3. 更新 docs/README.md 中的版本引用
    4. 更新 docs/guide/快速开始.md 中的 ASCII art 版本
    5. 更新 docs/CONTRIBUTING.md 中的版本示例

不处理:
    - packages/*/README.md 中的依赖版本约束 (如 fp-core>=0.1.0) ——
      这些是最低版本要求，不应随版本发布而变更
    - 包 pyproject.toml 的版本 —— 由 setuptools-scm 从 git tag 自动推导

依赖: 无 (纯 Python 标准库)
"""

import re
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 需要同步版本的目标文件及模式
TARGETS: list[dict] = [
    {
        "path": "docs/README.md",
        "pattern": r"(版本历史：)v?[\d.]+",
        "replacement": r"\g<1>{version}",
    },
    {
        "path": "docs/guide/快速开始.md",
        "pattern": r"(Five Pebbles Agent  )v?[\d.]+",
        "replacement": r"\g<1>{version}",
    },
    {
        "path": "docs/CONTRIBUTING.md",
        "pattern": r"(Agent 版本: )[\d.]+",
        "replacement": r"\g<1>{version}",
    },
    {
        "path": ".github/ISSUE_TEMPLATE/bug_report.md",
        "pattern": r"(Agent 版本: \[例如 )[\d.]+",
        "replacement": r"\g<1>{version}",
    },
]


def parse_args() -> tuple[str, str]:
    if len(sys.argv) < 2:
        print("用法: python scripts/bump_docs.py <版本号> [日期]")
        print("示例: python scripts/bump_docs.py 0.2.0")
        print("      python scripts/bump_docs.py 0.2.0 2026-07-01")
        sys.exit(1)

    version = sys.argv[1]
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        print(f"❌ 无效版本号: {version}，需符合 semver 格式 X.Y.Z")
        sys.exit(1)

    if len(sys.argv) >= 3:
        release_date = sys.argv[2]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", release_date):
            print(f"❌ 无效日期: {release_date}，需符合 YYYY-MM-DD 格式")
            sys.exit(1)
    else:
        release_date = date.today().isoformat()

    return version, release_date


def update_changelog(version: str, release_date: str) -> bool:
    """在 CHANGELOG.md 顶部插入新版本条目。"""
    path = REPO_ROOT / "docs" / "CHANGELOG.md"
    content = path.read_text(encoding="utf-8")

    # 查找插入点：第一个 `## [` 之前（版本说明之后）
    insert_marker = "\n## ["
    pos = content.find(insert_marker)
    if pos == -1:
        print("❌ CHANGELOG.md: 未找到版本条目起始标记")
        return False

    # 检查该版本是否已存在
    existing = content.find(f"## [{version}]")
    if existing != -1 and existing < content.find(insert_marker, pos + 1):
        print(f"⚠️  CHANGELOG.md: 版本 [{version}] 已存在，跳过插入")
    else:
        # 生成新版本条目
        new_entry = f"\n## [{version}] — {release_date}\n\n### Added\n\n- \n\n### Changed\n\n- \n\n### Fixed\n\n- \n"
        content = content[:pos] + new_entry + content[pos:]
        path.write_text(content, encoding="utf-8")
        print(f"✅ CHANGELOG.md: 已插入版本 [{version}] 条目")

    # 更新底部的发布历史表格
    # 查找表格位置（从前往后扫描行）
    lines = content.split("\n")
    table_start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("|") and line.count("|") >= 3:
            if table_start is None:
                table_start = i
        else:
            if table_start is not None:
                # 找到了表格范围 [table_start, i)
                break

    if table_start is not None:
        # 检查该版本是否已在表格中
        table_has_version = any(f"| {version} |" in line for line in lines[table_start:i])
        if table_has_version:
            print(f"⚠️  CHANGELOG.md: 发布历史表格中版本 [{version}] 已存在，跳过")
        else:
            # 在表格顶部插入新行
            new_row = f"| {version} | {release_date} | 待补充 |\n"
            lines.insert(table_start, new_row)
            content = "\n".join(lines)
            path.write_text(content, encoding="utf-8")
            print("✅ CHANGELOG.md: 已更新发布历史表格")
    else:
        print("⚠️  CHANGELOG.md: 未找到发布历史表格，跳过")

    return True


def update_version_refs(version: str) -> int:
    """更新各文档中的版本号引用。"""
    updated = 0

    for target in TARGETS:
        path = REPO_ROOT / target["path"]
        if not path.exists():
            print(f"⚠️  {target['path']}: 文件不存在，跳过")
            continue

        content = path.read_text(encoding="utf-8")
        pattern = target["pattern"]
        replacement = target["replacement"].format(version=version)

        new_content, count = re.subn(pattern, replacement, content)
        if count > 0:
            path.write_text(new_content, encoding="utf-8")
            print(f"✅ {target['path']}: 已更新 {count} 处")
            updated += count
        else:
            print(f"   {target['path']}: 未匹配到可替换内容")

    return updated


def main() -> None:
    version, release_date = parse_args()

    print("\n🔧 五块卵石 文档版本同步工具")
    print(f"   版本: {version}")
    print(f"   日期: {release_date}\n")

    # 1. 更新 CHANGELOG
    update_changelog(version, release_date)

    # 2. 更新文档引用
    count = update_version_refs(version)

    print("\n📊 汇总:")
    print(f"   - CHANGELOG: {'✅ 已更新' if True else '❌'}")
    print(f"   - 文档引用: {count} 处已更新")
    print("\n📝 接下来: 编辑 CHANGELOG.md 补充版本变更内容后提交")
    print(f"   git tag {version} && git push --tags")


if __name__ == "__main__":
    main()
