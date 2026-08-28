"""ext git — git 封装

规则：
  - private 仓库禁 remote（检测到 remote 直接报错拒绝，防误推送私有资产）
  - public 仓库可 push（分享通道）
  - 所有仓库自动 git init（迁移时一步到位）
"""

import os
import subprocess

from fp_core.logger import get_logger


class GitError(RuntimeError):
    pass


def run_git(cwd: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """在指定目录执行 git 命令。失败抛 GitError。"""
    cmd = ["git", *args]
    try:
        proc: subprocess.CompletedProcess[str] = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(f"git 执行失败: {e}") from e
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} 失败: {proc.stderr.strip()}")
    return proc


def ensure_repo(path: str) -> bool:
    """确保目录是 git 仓库（不存在 .git 则自动 init），返回是否新建。"""
    if os.path.isdir(os.path.join(path, ".git")):
        return False
    os.makedirs(path, exist_ok=True)
    run_git(path, "init", "-q")
    get_logger().info(f"[ext] git 仓库已初始化: {path}")
    return True


def has_remote(path: str) -> bool:
    """检测仓库是否配置了 remote（private 仓库不允许）。"""
    proc = run_git(path, "remote", "-v", check=False)
    return bool(proc.stdout.strip())


def ensure_no_remote(path: str) -> None:
    """private 仓库守卫：检测到 remote 直接报错拒绝。"""
    if has_remote(path):
        raise GitError(f"私有仓库不允许配置 remote: {path}（私有资产禁止外推）")


def commit_all(path: str, message: str, user_name: str = "") -> None:
    """add 全部 + commit。无变化时静默跳过。"""
    if not os.path.isdir(os.path.join(path, ".git")):
        return
    run_git(path, "add", "-A")
    proc = run_git(path, "status", "--porcelain", check=False)
    if not proc.stdout.strip():
        return
    extra: list[str] = []
    if user_name:
        extra += ["-c", f"user.name={user_name}", "-c", "user.email=fp@local"]
    run_git(path, *extra, "commit", "-q", "-m", message)


def status_short(path: str) -> str:
    """返回 git 状态摘要（porcelain），无仓库返回空串。"""
    if not os.path.isdir(os.path.join(path, ".git")):
        return ""
    proc = run_git(path, "status", "--porcelain", check=False)
    return proc.stdout.strip()


def list_files(path: str) -> list[str]:
    """返回仓库跟踪文件列表（相对路径）。"""
    if not os.path.isdir(os.path.join(path, ".git")):
        return []
    proc = run_git(path, "ls-files", check=False)
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]
