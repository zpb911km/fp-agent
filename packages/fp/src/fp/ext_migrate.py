"""ext migrate — 存量迁移（识别即迁移，无兼容双轨）

原则：core 只认三目录（fetched/public/private），老结构路径（{DATA}/tools、
{DATA}/commands、{DATA}/plugins、{DATA}/memory）检测到后自动移动文件到
private/ 对应子目录，迁移后老目录删除。

ensure() 幂等：fp main() 进入界面模式前调用；无老结构时直接通过（零开销）。
ext_migrate 只做文件移动 + git init，不 import 任何 fp_core 加载逻辑。
"""

import contextlib
import os
import shutil

from fp.ext_assets import (
    SOURCES,
    detect_legacy,
    source_dir,
    source_root,
)
from fp.ext_git import ensure_no_remote, ensure_repo
from fp_core.logger import get_logger


def _unique_target(dest_dir: str, fname: str) -> str:
    """同名冲突时返回加后缀的目标路径（保留两者，不覆盖）。"""
    candidate = os.path.join(dest_dir, fname)
    if not os.path.exists(candidate):
        return candidate
    base, ext = os.path.splitext(fname)
    n = 1
    while True:
        candidate = os.path.join(dest_dir, f"{base}.legacy_{n}{ext}")
        if not os.path.exists(candidate):
            return candidate
        n += 1


def _move_file(src: str, dest_dir: str) -> str:
    """移动单个文件到目标目录，冲突自动改名，返回最终路径。"""
    os.makedirs(dest_dir, exist_ok=True)
    dest = _unique_target(dest_dir, os.path.basename(src))
    shutil.move(src, dest)
    return dest


def migrate_once() -> list[str]:
    """执行一次存量迁移，返回迁移报告行列表。空 = 无需迁移。"""
    report: list[str] = []
    legacy = detect_legacy()
    if not legacy:
        return report

    get_logger().info(f"[ext] 检测到老结构目录 {len(legacy)} 处，开始迁移 → private/")

    for asset_type, legacy_path in legacy:
        dest_dir = source_dir("private", asset_type)
        moved = 0
        for fname in sorted(os.listdir(legacy_path)):
            src = os.path.join(legacy_path, fname)
            # 跳过内部目录（__pycache__/.git 等）与隐藏文件
            if fname in ("__pycache__", ".git") or fname.startswith("."):
                continue
            if os.path.isdir(src):
                # 目录（如插件包）整体移动
                os.makedirs(dest_dir, exist_ok=True)
                dest = _unique_target(dest_dir, fname)
                shutil.move(src, dest)
                report.append(f"  {asset_type}: {fname}/ → private/{fname}/")
                moved += 1
            elif os.path.isfile(src):
                dest = _move_file(src, dest_dir)
                report.append(f"  {asset_type}: {fname} → private/{os.path.basename(dest)}")
                moved += 1
        # 迁移后清理老目录（空目录直接删）
        with contextlib.suppress(OSError):
            shutil.rmtree(legacy_path)
        if moved:
            report.append(f"  {asset_type}: 共迁移 {moved} 项")

    # git 自动初始化（private/public，含 fetched——供审计一致性）
    for source in SOURCES:
        root = source_root(source)
        os.makedirs(root, exist_ok=True)
        if source == "private":
            ensure_no_remote(root)  # 防御：已存在 remote 则拒绝
        ensure_repo(root)

    get_logger().info("[ext] 迁移完成：老结构已并入 private/，git 仓库已初始化")
    return report


def ensure() -> None:
    """幂等入口：fp 启动前调用。有老结构则自动迁移，无则零开销通过。"""
    report = migrate_once()
    if report:
        for line in report:
            get_logger().info(f"[ext]{line}")
