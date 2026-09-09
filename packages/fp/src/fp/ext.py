"""fp ext — 扩展资产分发 CLI

管道工具（零 core 依赖、零 LLM 逻辑）：进出管道，审查在会话里，决策权在用户。

命令集：
  fetch <url|path>   拉取资产到暂存区（staging）+ 静态扫描 + 登记 pending_review
  review <name>      记录审查结论（审计第二段）：--approve / --reject
  install <name>     落地到 fetched/（门禁：须先 review --approve）
  list               列出三来源所有资产
  info <name>        查看单个资产详情
  remove <name>      删除资产（进 .trash 可恢复）
  update <name>      重新拉取并覆盖已安装的 fetched 资产
  check              存量体检：unmanaged 资产 + 静态扫描 + 审计核对
  new <type> <name>  生成新资产脚手架（到 private/）
  init <dir>         为已有资产补充/校验 __fp__ manifest
  promote <name>     私有 → 公开（复制快照到 public/ + 隐私扫描 + 登记清单 + git）
  demote <name>      公开 → 私有（从 public/ 收回发布，private 原件保留）
  share [--push]     发布 public/ 仓库：校验库清单 + __fp__ + 孤儿文件 + 代码检查 + commit + push
  migrate            手动执行存量迁移（老结构 → private/）
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from typing import TYPE_CHECKING, Any, NoReturn, TypedDict, cast

from fp.ext_assets import (
    ASSET_TYPES,
    SOURCES,
    registry_path,
    source_dir,
    source_root,
    trash_dir,
)
from fp.ext_git import GitError, commit_all, ensure_repo, has_remote, run_git
from fp.ext_manifest import (
    parse_fp_manifest,
    parse_memory_manifest,
    parse_tool_name,
    validate_manifest,
)
from fp.ext_migrate import migrate_once
from fp.ext_scanner import format_report, scan_directory, scan_file
from fp.ext_store import (
    append_audit,
    load_audit,
    load_registry,
    remove_asset,
    save_registry,
    upsert_asset,
)

if TYPE_CHECKING:
    from fp.ext_scanner import Hit


# 暂存区（fetch 后的审查场所）——动态获取，避免模块级绑定导致路径固化（测试/换环境时残留）
def _staging_dir() -> str:
    return os.path.join(os.path.dirname(registry_path()), ".staging")


# public 仓库的库级清单文件名（share 校验/维护用，位于 {DATA}/public/ 根目录）
SHARE_INDEX = "fp.ext.json"


# ── 类型定义（资产分发内部数据结构） ──────────────────────────────
class _PublicIndex(TypedDict, total=False):
    """public 库级清单（fp.ext.json）。"""

    schema: int
    author: str
    license: str
    assets: dict[str, Any]


class _PublicAsset(TypedDict):
    """public/ 下枚举出的磁盘资产。"""

    atype: str
    name: str | None
    path: str
    manifest: dict[str, Any] | None


class _RepoAsset(TypedDict):
    """暂存区（仓库/目录/单文件）中扫描出的资产。"""

    type: str
    name: str
    relpath: str
    manifest: dict[str, Any] | None


class _Provenance(TypedDict, total=False):
    """fetch 来源信息。"""

    type: str
    source: str
    commit: str
    sha256: str


# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_git_url(src: str) -> bool:
    return src.startswith(("http://", "https://", "git@", "ssh://")) or src.endswith(".git")


def _is_single_file_url(src: str) -> bool:
    return src.startswith(("http://", "https://")) and src.rsplit("/", 1)[-1].endswith(".py")


def _staging_path(name: str) -> str:
    return os.path.join(_staging_dir(), name)


def _ensure_staging(name: str) -> str:
    path = _staging_path(name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


def _find_main_file(d: str, atype: str) -> str | None:
    """在资产目录 d 中定位该类型的主文件（目录型资产）。

    规则与 cmd_init 一致：tools 取 *_plugin.py，commands 取非 _ 开头的 .py，
    plugins 取 __init__.py，memory 取 .md。
    """
    if not os.path.isdir(d):
        return None
    if atype == "tools":
        for f in os.listdir(d):
            if f.endswith("_plugin.py"):
                return os.path.join(d, f)
    elif atype == "commands":
        for f in os.listdir(d):
            if f.endswith(".py") and not f.startswith("_"):
                return os.path.join(d, f)
    elif atype == "plugins":
        init = os.path.join(d, "__init__.py")
        if os.path.isfile(init):
            return init
    else:  # memory
        for f in os.listdir(d):
            if f.endswith(".md"):
                return os.path.join(d, f)
    return None


def _asset_filepath(d: str, name: str, atype: str) -> str | None:
    """在资产目录 d 中定位资产路径（支持单文件与目录型资产）。

    优先级：单文件匹配 → <name>/ 目录型 → manifest name 兜底。
    """
    # 1. 单文件匹配
    candidates = [name, f"{name}.py", f"{name}.md"]
    if atype == "tools":
        candidates.append(f"{name}_plugin.py")
    for c in candidates:
        p = os.path.join(d, c)
        if os.path.isfile(p):
            return p
    # 2. 目录型：<name>/ 下找主文件
    pdir = os.path.join(d, name)
    if os.path.isdir(pdir):
        main = _find_main_file(pdir, atype)
        if main:
            return main
    # 3. manifest name 兜底：文件名/目录名 ≠ 语义名（如 codegraph_query_plugin.py → name=codegraph）
    if os.path.isdir(d):
        for e in sorted(os.listdir(d)):
            if e.startswith(".") or e == "__pycache__" or e.endswith(".disabled"):
                continue
            p = os.path.join(d, e)
            if os.path.isfile(p) and (p.endswith(".py") or p.endswith(".md")):
                if atype == "tools" and p.endswith(".py"):
                    if parse_tool_name(p) == name:
                        return p
                else:
                    m = parse_fp_manifest(p) if p.endswith(".py") else parse_memory_manifest(p)
                    if m and m.get("name") == name:
                        return p
            elif os.path.isdir(p) and p != pdir:
                main = _find_main_file(p, atype)
                if main:
                    m = parse_fp_manifest(main) if main.endswith(".py") else parse_memory_manifest(main)
                    if m and m.get("name") == name:
                        return main
    return None


def _find_asset(name: str) -> tuple[str, str] | None:
    """在三来源中查找资产，返回 (来源, 类型)。优先级 private > public > fetched。"""
    for source in reversed(SOURCES):  # private 优先
        for atype in ASSET_TYPES:
            if _asset_filepath(source_dir(source, atype), name, atype):
                return source, atype
    return None


def _asset_display(source: str, atype: str, name: str) -> str:
    return f"{source}/{atype}/{name}"


# ═══════════════════════════════════════════════════════════════
# public 仓库（单仓库模型）辅助
# ═══════════════════════════════════════════════════════════════


def _public_index_path() -> str:
    """public 仓库的库级清单路径（{DATA}/public/fp.ext.json）。"""
    return os.path.join(source_root("public"), SHARE_INDEX)


def _load_public_index() -> _PublicIndex | None:
    """读取 public 库级清单；不存在或 JSON 损坏返回 None。"""
    path = _public_index_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    return cast(_PublicIndex, data) if isinstance(data, dict) else None


def _save_public_index(index: _PublicIndex) -> None:
    """写 public 库级清单（原子写）。"""
    path = _public_index_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _register_asset_in_index(atype: str, name: str, commit: bool = True) -> bool:
    """把资产登记进 public 库清单（幂等）。清单不存在时跳过（不自动创建）。

    返回是否实际改动。promote/demote 后调用，保持"清单即发布物目录"一致。
    """
    index = _load_public_index()
    if index is None:
        return False
    key = f"{atype}/{name}"
    assets = cast(dict[str, Any], index).setdefault("assets", {})
    if key in assets:
        return False
    assets[key] = {
        "source": name,
        "updated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }
    _save_public_index(index)
    if commit:
        ensure_repo(source_root("public"))
        commit_all(source_root("public"), f"index: register {key}")
    return True


def _unregister_asset_in_index(atype: str, name: str, commit: bool = True) -> bool:
    """从 public 库清单移除资产登记（幂等）。清单不存在时跳过。"""
    index = _load_public_index()
    if index is None:
        return False
    key = f"{atype}/{name}"
    if key not in index.get("assets", {}):
        return False
    del cast(dict[str, Any], index)["assets"][key]
    _save_public_index(index)
    if commit:
        ensure_repo(source_root("public"))
        commit_all(source_root("public"), f"index: unregister {key}")
    return True


def _scan_public_assets() -> list[_PublicAsset]:
    """枚举 public/ 下所有资产。

    返回 [{atype, name, path, manifest}]：
      name 为 None → 无法解析（缺 __fp__ 也无工具定义，孤儿候选）；
      manifest 为 None → 缺 __fp__（文件级协议缺失，share 拒绝）。
    """
    assets: list[_PublicAsset] = []
    for atype in ASSET_TYPES:
        d = source_dir("public", atype)
        if not os.path.isdir(d):
            continue
        for e in sorted(os.listdir(d)):
            if e.startswith(".") or e == "__pycache__" or e.endswith(".disabled"):
                continue
            fpath = os.path.join(d, e)
            manifest: dict[str, Any] | None = None
            name: str | None = None
            if os.path.isfile(fpath):
                if fpath.endswith(".py"):
                    if atype == "tools":
                        manifest = parse_fp_manifest(fpath)
                        name = parse_tool_name(fpath)
                    else:
                        manifest = parse_fp_manifest(fpath)
                        name = manifest.get("name") if manifest else None
                elif fpath.endswith(".md"):
                    manifest = parse_memory_manifest(fpath)
                    name = manifest.get("name") if manifest else None
            elif os.path.isdir(fpath):
                main = _find_main_file(fpath, atype)
                if main:
                    manifest = parse_fp_manifest(main) if main.endswith(".py") else parse_memory_manifest(main)
                    name = manifest.get("name") if manifest else None
            assets.append({"atype": atype, "name": name, "path": fpath, "manifest": manifest})
    return assets


def _check_public_orphans(assets: list[_PublicAsset], index: _PublicIndex) -> list[str]:
    """孤儿文件检查，返回问题列表（空 = 干净）。

    覆盖三类：
      ① 根目录杂散项（非 fp.ext.json、非类型目录）
      ② 清单登记但磁盘缺失（悬空）
      ③ 磁盘资产未登记 / 无法解析的杂散文件
    """
    issues: list[str] = []
    public_root = source_root("public")

    # ① 根目录杂散项
    for e in sorted(os.listdir(public_root)):
        if e.startswith(".") or e == SHARE_INDEX:
            continue
        p = os.path.join(public_root, e)
        if os.path.isdir(p) and e in ASSET_TYPES:
            continue
        issues.append(f"public/ 根目录杂散项: {e}")

    # ② 清单登记但磁盘缺失（悬空）
    declared = index.get("assets", {})
    for key in declared:
        if "/" not in key:
            issues.append(f"清单登记格式错误: {key}")
            continue
        atype, name = key.split("/", 1)
        if atype not in ASSET_TYPES:
            issues.append(f"清单登记未知类型: {key}")
            continue
        if not _asset_filepath(source_dir("public", atype), name, atype):
            issues.append(f"清单登记但文件缺失: {key}")

    # ③ 磁盘资产未登记 / 杂散文件
    disk_keys = {f"{a['atype']}/{a['name']}" for a in assets if a["name"]}
    for key in sorted(disk_keys - set(declared)):
        issues.append(f"资产未在 {SHARE_INDEX} 登记: {key}")
    for a in assets:
        if not a["name"]:
            issues.append(f"无法识别资产（缺 __fp__ / 工具定义）: {a['atype']}/{os.path.basename(a['path'])}")
    return issues


# ═══════════════════════════════════════════════════════════════
# 命令实现
# ═══════════════════════════════════════════════════════════════


def _scan_repo_assets(staging: str) -> list[_RepoAsset]:
    """扫描暂存区（仓库/目录/单文件）中的**全部**资产。

    返回 [{type, name, relpath, manifest}]，relpath 相对 staging：
      - 单文件资产（tools/extensions/foo_plugin.py、commands/bar.py、memory/memo.md）→ relpath=文件
      - 目录型资产（plugins/baz/__init__.py、tools/foo/foo_plugin.py 且父目录名=name）→ relpath=目录
    跳过 .git / 隐藏项 / __pycache__ / .disabled。manifest 缺失的文件不视为资产。
    """
    assets: list[_RepoAsset] = []
    seen: set[str] = set()
    for root, dirs, files in os.walk(staging):
        # 跳过 .git / 隐藏目录 / __pycache__
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        for fname in sorted(files):
            if fname.startswith(".") or fname.endswith(".disabled"):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, staging)
            if fname.endswith(".py"):
                m = parse_fp_manifest(fpath)
                if not m or m.get("type") not in ASSET_TYPES or not m.get("name"):
                    continue
                atype, name = m["type"], m["name"]
                key = f"{atype}/{name}"
                if key in seen:
                    continue
                # 目录型判定：主文件父目录名 == 资产名 且非 staging 根
                # （如 plugins/baz/、tools/foo/；staging 根 = 单文件 URL/本地源暂存，
                #   目录名恰好等于资产名不算目录型，否则 install 会把整仓当目录落地）
                parent = os.path.dirname(fpath)
                is_asset_dir = os.path.basename(parent) == name and os.path.relpath(parent, staging) != "."
                relpath = os.path.relpath(parent, staging) if is_asset_dir else rel
                assets.append({"type": atype, "name": name, "relpath": relpath, "manifest": m})
                seen.add(key)
            elif fname.endswith(".md"):
                m = parse_memory_manifest(fpath)
                if not m or not m.get("name"):
                    continue
                key = f"memory/{m['name']}"
                if key in seen:
                    continue
                assets.append({"type": "memory", "name": m["name"], "relpath": rel, "manifest": m})
                seen.add(key)
    return assets


def _do_fetch(src: str, mode: str = "fetch") -> tuple[int, str | None, _Provenance, list[_RepoAsset]]:
    """核心拉取逻辑。

    返回 (rc, staging, provenance, assets)：
      rc        退出码（0 成功）
      staging   暂存区路径
      provenance 来源信息（type/source/commit/sha256）
      assets    仓库内全部资产清单 [{type, name, relpath, manifest}]

    mode="update"：已 active 资产重新拉取后标记 reviewed（待重新落地），不回落 pending_review。
    """
    staging: str | None = None
    provenance: _Provenance = {}

    if _is_git_url(src):
        staging = _ensure_staging("fetch_git")
        run_git(staging, "clone", "--depth", "1", src, ".")
        commit = run_git(staging, "rev-parse", "HEAD").stdout.strip()
        provenance = {"type": "git", "source": src, "commit": commit}
    elif _is_single_file_url(src):
        name = src.rsplit("/", 1)[-1][:-3]
        staging = _ensure_staging(name)
        target = os.path.join(staging, f"{name}_plugin.py" if not name.endswith("_plugin") else f"{name}.py")
        import urllib.request

        with urllib.request.urlopen(src, timeout=30) as r, open(target, "wb") as f:
            f.write(r.read())
        provenance = {"type": "file-url", "source": src, "commit": "", "sha256": _sha256(target)}
    elif os.path.exists(src):
        name = os.path.basename(src.rstrip("/"))
        staging = _ensure_staging(name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(staging, name), dirs_exist_ok=True)
        else:
            shutil.copy2(src, staging)
        provenance = {
            "type": "local",
            "source": os.path.abspath(src),
            "commit": "",
            "sha256": _sha256(src) if os.path.isfile(src) else "",
        }
    else:
        return 1, None, {}, []

    # 扫描仓库内全部资产 → 逐个登记 pending_review（共享 staging/source/commit）
    assets = _scan_repo_assets(staging)
    now = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    for a in assets:
        key = f"{a['type']}/{a['name']}"
        existing = load_registry()["assets"].get(key)
        status = "pending_review"
        if mode == "update" and existing and existing.get("status") == "active":
            status = "reviewed"  # update：重新拉取后待重新落地
        reg_entry = {
            "name": a["name"],
            "type": a["type"],
            "source": provenance.get("source", ""),
            "commit": provenance.get("commit", ""),
            "sha256": provenance.get("sha256", ""),
            "fetched_at": now,
            "staging": staging,
            "relpath": a["relpath"],
            "status": status,
        }
        upsert_asset(key, reg_entry)
    return 0, staging, provenance, assets


def cmd_fetch(args: argparse.Namespace) -> int:
    """拉取仓库/包到暂存区 + 静态扫描 + 登记全部资产 pending_review。

    单位是「仓库」（可含多个资产）；安装单位是「资产」。
    fetch 只负责拉取与登记，审查与选择在对话层由用户拍板后 review/install。
    """
    rc, staging, provenance, assets = _do_fetch(args.source)
    if rc != 0:
        print(f"❌ 无法识别的来源: {args.source}")
        return 1

    print(f"📥 已拉取到暂存区: {staging}")
    commit = provenance.get("commit")
    if commit:
        print(f"   commit: {commit[:12]}")
    sha256 = provenance.get("sha256")
    if sha256:
        print(f"   sha256: {sha256[:16]}…")
    print(f"   发现 {len(assets)} 个资产:" if assets else "   未发现带 __fp__ 协议声明的资产（仍可审查后手动 install）")
    for a in assets:
        m: dict[str, Any] = a["manifest"] or {}
        print(f"   · {a['type']}/{a['name']} v{m.get('version', '?')} — {m.get('description', '')}")

    print("\n── 静态扫描 ──")
    print(format_report(scan_directory(cast(str, staging))))

    if assets:
        print("\n⏭  下一步（选择想安装的资产，逐个走审查门禁）：")
        for a in assets:
            print(f'   fp ext review {a["name"]} --approve --note "审查意见"  →  fp ext install {a["name"]}')
    else:
        print("\n⏭  下一步：审查暂存区源码后，执行 fp ext install <name> 落地。")
    return 0


def _standard_asset_filename(atype: str, name: str) -> str | None:
    """单文件资产落地 fetched/ 时的标准文件名（对齐 core 加载器识别约定）。

    tools → <name>_plugin.py（保持 *_plugin.py 后缀约定）
    commands → <name>.py
    memory → <name>.md
    plugins → <name>.py（plugins 目录包走目录型 <name>/，不在此列）
    """
    safe = name.replace("/", "_")
    if atype == "tools":
        return f"{safe}_plugin.py" if not safe.endswith("_plugin") else f"{safe}.py"
    if atype == "commands":
        return f"{safe}.py"
    if atype == "memory":
        return f"{safe}.md"
    if atype == "plugins":
        return f"{safe}.py"
    return None


def _dir_has_extra_entries(d: str, main: str) -> bool:
    """资产目录 d 除主文件 main 外是否还有实质内容（附属文件/子目录）。

    无附加 → 可把主文件抽出按单文件落地（防「纯包装目录被目录化」）；
    有附加 → 须整体目录搬迁（阶段二：多文件扩展暂不落地单文件）。
    """
    for e in os.listdir(d):
        if e.startswith(".") or e == "__pycache__" or e.endswith(".disabled"):
            continue
        if os.path.join(d, e) != main:
            return True
    return False


def _find_asset_in_staging(staging: str, atype: str, name: str) -> str | None:
    """在暂存区中按类型/名称定位资产本体（fallback：registry 无 relpath 时）。"""
    for root, _dirs, files in os.walk(staging):
        if ".git" in root.split(os.sep):
            continue
        for fname in sorted(files):
            if fname.startswith(".") or fname.endswith(".disabled"):
                continue
            fpath = os.path.join(root, fname)
            if not (fname.endswith(".py") or fname.endswith(".md")):
                continue
            if atype == "tools" and fname.endswith("_plugin.py"):
                if parse_tool_name(fpath) == name:
                    return fpath
            else:
                m = parse_fp_manifest(fpath) if fname.endswith(".py") else parse_memory_manifest(fpath)
                if m and m.get("name") == name:
                    return fpath
    return None


def cmd_install(args: argparse.Namespace) -> int:
    """从暂存区提取**资产本体**落地到 fetched/（门禁：须先 review --approve）。

    与 fetch（单位=仓库）不同，install 的单位=资产：只提取该资产本体，
    不带 .git、不带仓库嵌套结构。落地形态与 private/public 及 core 加载器
    约定一致：
      - 单文件资产（tools/commands/memory/plugins）→ 单文件标准名，
        保持加载器可自发现（tools/<name>_plugin.py、commands/<name>.py、
        memory/<name>.md、plugins/<name>.py）；
      - 目录型资产（plugins 目录包，或含附属文件的多文件资产）→ 目录 <name>/。
    （历史 bug：曾统一落地为 fetched/<type>/<name>/ 目录，而 commands/tools/memory
    加载器按单层 *.py / *_plugin.py / *.md 扫描，导致安装后无法自发现。）
    """
    name = args.name
    found = _find_reg_entry_by_name(name)
    entry_key = found[0] if found else None
    reg_entry = found[1] if found else None

    # 确定 staging 与 relpath（registry 记录优先；fallback 按暂存区名）
    staging = reg_entry.get("staging") if reg_entry else None
    relpath = reg_entry.get("relpath") if reg_entry else None
    atype = (cast(dict[str, Any], reg_entry).get("type") or entry_key.split("/", 1)[0]) if entry_key else "tools"
    if not staging or not os.path.isdir(staging):
        staging = None
        for cand in (_staging_path(name), _staging_path("fetch_git")):
            if os.path.isdir(cand):
                staging = cand
                break
    if not staging or not os.path.isdir(staging):
        print("❌ 暂存区不存在（请先 fp ext fetch）")
        return 1

    # ── 三阶段门禁：审查状态校验 ──
    if reg_entry:
        status = reg_entry.get("status")
        if status == "pending_review":
            print("❌ 该资产尚未审查（status=pending_review）。")
            print("   请先在会话中审查暂存区源码，用户拍板后执行：")
            print(f'   fp ext review {name} --approve --note "审查意见"')
            return 1
        if status == "rejected":
            print("❌ 该资产已被拒绝安装（status=rejected）。")
            print(f'   如需重新审查：fp ext review {name} --approve --note "复核通过"')
            return 1

    # 定位资产本体（registry relpath 优先，fallback walk 查找）
    src_body: str | None = None
    if relpath:
        cand = os.path.join(staging, relpath)
        if os.path.exists(cand):
            src_body = cand
    if src_body is None:
        src_body = _find_asset_in_staging(staging, atype, name)
    if src_body is None:
        print(f"❌ 在暂存区未找到资产本体: {name}（{staging}）")
        return 1

    # ── 落地形态对齐 core 加载器（fetched 与 private/public 同构） ──
    # 单文件资产 → 标准单文件（名称规范化到语义名，杜绝「多一层目录」；
    #   目录仅当 src_body 是纯包装目录时抽主文件，否则整体搬目录）。
    dest_root = source_dir("fetched", atype)
    os.makedirs(dest_root, exist_ok=True)

    if os.path.isdir(src_body):
        # 目录型本体：plugins 目录包 / 多文件资产 → 目录整体落地；
        # tools/commands/memory 的「纯包装目录」（仅含主文件，如本地目录源）
        # → 抽出主文件按单文件落地，保证自发现。
        main = _find_main_file(src_body, atype)
        if atype != "plugins" and main and not _dir_has_extra_entries(src_body, main):
            dest = os.path.join(dest_root, _standard_asset_filename(atype, name) or os.path.basename(main))
            if os.path.exists(dest) and not args.force:
                print(f"⚠️  已存在: {dest}")
                print("   使用 --force 覆盖（同 source 重新安装），或先 fp ext remove。")
                return 1
            shutil.copy2(main, dest)
        else:
            dest = os.path.join(dest_root, name)
            if os.path.exists(dest) and not args.force:
                print(f"⚠️  已存在: {dest}")
                print("   使用 --force 覆盖（同 source 重新安装），或先 fp ext remove。")
                return 1
            shutil.rmtree(dest, ignore_errors=True)
            os.makedirs(dest, exist_ok=True)
            for item in os.listdir(src_body):
                s = os.path.join(src_body, item)
                d = os.path.join(dest, item)
                if os.path.isdir(s):
                    shutil.copytree(s, d, dirs_exist_ok=True)
                else:
                    shutil.copy2(s, d)
            if atype != "plugins":
                print(
                    f"⚠️  该资产为多文件目录型，{atype} 加载器当前只识别单文件，"
                    "安装后无法自动发现（多文件扩展属阶段二能力）。"
                )
    else:
        # 单文件本体 → 标准单文件落地
        dest = os.path.join(dest_root, _standard_asset_filename(atype, name) or os.path.basename(src_body))
        if os.path.exists(dest) and not args.force:
            print(f"⚠️  已存在: {dest}")
            print("   使用 --force 覆盖（同 source 重新安装），或先 fp ext remove。")
            return 1
        shutil.copy2(src_body, dest)

    # 注册表（保留审查字段；无 registry 记录则构造新条目）
    now = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    if reg_entry:
        reg_entry["status"] = "active"
        reg_entry["installed_at"] = now
        upsert_asset(cast(str, entry_key), reg_entry)
        source_origin = reg_entry.get("source", "")
    else:
        source_origin = args.source or ""
        reg_entry = {
            "name": name,
            "type": atype,
            "source": source_origin,
            "commit": "",
            "sha256": "",
            "installed_at": now,
            "status": "active",
        }
        upsert_asset(f"{atype}/{name}", reg_entry)
    append_audit("install", f"{atype}/{name}", origin=source_origin)
    print(f"✅ 已安装: {_asset_display('fetched', atype, name)}")
    print(f"   落地: {dest}")
    print(f"   注册表: {atype}/{name} [active]")
    print("🔁 提示: 加载器在会话启动时扫描资产，新会话或 /reload 后生效")
    return 0


def _find_reg_entry_by_name(name: str) -> tuple[str, dict[str, Any]] | None:
    """按资产名（或 key）在 registry 中查找，返回 (key, entry)。"""
    reg = load_registry()
    # 先按 key 精确匹配
    if f"tools/{name}" in reg["assets"]:
        return f"tools/{name}", reg["assets"][f"tools/{name}"]
    for k, v in reg["assets"].items():
        if v.get("name") == name or k == name or k.endswith(f"/{name}"):
            return k, v
    return None


def _mark_reviewed(key: str, note: str) -> None:
    """将 registry 条目标记为 reviewed（审计第二段：FP 判断）。"""
    reg = load_registry()
    entry = reg["assets"][key]
    entry["status"] = "reviewed"
    entry["reviewed_at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    entry["note"] = note
    save_registry(reg)
    append_audit("review", key, origin=entry.get("source", ""), note=note)


def cmd_review(args: argparse.Namespace) -> int:
    """记录 FP 对暂存资产的审查结论（审计第二段）。"""
    name = args.name
    found = _find_reg_entry_by_name(name)
    if found is None:
        print(f"❌ 未找到待审查资产: {name}（先 fp ext fetch）")
        return 1
    key, entry = found

    if args.approve:
        _mark_reviewed(key, args.note)
        print(f"✅ 已记录审查通过: {key}")
        print(f"   下一步：fp ext install {name}")
        return 0
    if args.reject:
        reg = load_registry()
        reg["assets"][key]["status"] = "rejected"
        reg["assets"][key]["note"] = args.note
        save_registry(reg)
        append_audit("reject", key, origin=entry.get("source", ""), note=args.note)
        print(f"⛔ 已记录审查拒绝: {key}")
        return 0

    # 无标记 → 显示详情
    print(f"📋 待审查: {key} [status={entry.get('status')}]")
    for k, v in entry.items():
        if k == "staging":
            continue
        print(f"   {k}: {v}")
    if os.path.isdir(entry.get("staging", "")):
        print(f"   暂存区: {entry['staging']}（会话中用 read_file 审查源码）")
    print("\n   用法:")
    print(f'   fp ext review {name} --approve --note "审查意见"   # 通过')
    print(f'   fp ext review {name} --reject  --note "理由"      # 拒绝')
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """列出三来源所有资产。"""
    print("── 资产清单（三来源） ──")
    total = 0
    for source in SOURCES:
        lines: list[str] = []
        for atype in ASSET_TYPES:
            d = source_dir(source, atype)
            if not os.path.isdir(d):
                continue
            for e in sorted(os.listdir(d)):
                if e.startswith(".") or e == "__pycache__" or e.endswith(".disabled"):
                    continue
                # 解析资产名（manifest name 优先，否则文件名）
                display = e
                fpath = os.path.join(d, e)
                if os.path.isfile(fpath):
                    if fpath.endswith(".py") and atype == "tools":
                        tname = parse_tool_name(fpath)
                        m = {"name": tname} if tname else None
                    elif fpath.endswith(".py"):
                        m = parse_fp_manifest(fpath)
                    elif fpath.endswith(".md"):
                        m = parse_memory_manifest(fpath)
                    else:
                        m = None
                    if m and m.get("name"):
                        base = e.rsplit(".", 1)[0]
                        display = f"{m['name']} ({e})" if m["name"] != base else base
                lines.append(f"  {atype}/{display}")
                total += 1
        if lines:
            print(f"◉ {source}/  [{source_root(source)}]")
            print("\n".join(lines))
    if total == 0:
        print("（空）")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    """查看单个资产详情。"""
    name = args.name
    found = _find_asset(name)
    if not found:
        print(f"❌ 未找到资产: {name}")
        return 1
    source, atype = found
    d = source_dir(source, atype)
    fpath = _asset_filepath(d, name, atype)

    print(f"📦 {_asset_display(source, atype, name)}")
    if fpath:
        if os.path.isfile(fpath) and fpath.endswith(".py"):
            m = parse_fp_manifest(fpath)
        elif os.path.isfile(fpath) and fpath.endswith(".md"):
            m = parse_memory_manifest(fpath)
        else:
            m = None
        if m:
            for k, v in m.items():
                print(f"   {k}: {v}")
        else:
            print("   （无 manifest）")

    key = f"{atype}/{name}"
    reg = load_registry()["assets"].get(key)
    if reg:
        print(
            f"\n   registry: status={reg.get('status')} source={reg.get('source')} commit={reg.get('commit', '')[:12]}"
        )
    # 静态扫描
    if fpath and os.path.isfile(fpath) and fpath.endswith(".py"):
        hits = scan_file(fpath)
        print("\n── 静态扫描 ──")
        print(format_report({os.path.basename(fpath): hits}))
    return 0


def _asset_paths(d: str, name: str, atype: str) -> list[str]:
    """定位资产的实际路径：目录型返回资产目录，单文件返回文件。

    promote/demote/remove 移动时按此取路径，保证目录型资产整体迁移（不只主文件）。
    """
    pdir = os.path.join(d, name)
    if os.path.isdir(pdir) and _find_main_file(pdir, atype):
        return [pdir]
    p = _asset_filepath(d, name, atype)
    return [p] if p else []


def cmd_remove(args: argparse.Namespace) -> int:
    """删除资产（进 .trash 可恢复）。

    优先级语义：
      - fetched 已安装（registry active/installed_at）：删 fetched 本体 + registry，
        **不碰** public/private 同名资产（_find_asset 优先级会误伤 public）
      - 未安装待审查：仅清 registry（共享 staging 保留，供同仓库其他资产继续审查）
      - private/public 资产：删文件/目录 + registry + 自动 commit
    """
    name = args.name
    found = _find_reg_entry_by_name(name)
    if found:
        key, entry = found
        status = entry.get("status")
        installed_at = entry.get("installed_at")
        if status in ("pending_review", "rejected") and not installed_at:
            # 未安装待审查：仅移除 registry（staging 可能被同仓库其他资产共享，无引用才删）
            staging = entry.get("staging")
            if staging and os.path.isdir(staging):
                others = [v for k2, v in load_registry()["assets"].items() if k2 != key and v.get("staging") == staging]
                if not others:
                    shutil.rmtree(staging, ignore_errors=True)
            remove_asset(key)
            append_audit("remove", key, note="清理未安装的待审查资产")
            print(f"🗑  已清理待审查资产: {key}")
            return 0
        if installed_at:
            # fetched 已安装：删本体 + registry（同名 public/private 不受影响）
            atype = entry.get("type") or key.split("/", 1)[0]
            d = source_dir("fetched", atype)
            paths = _asset_paths(d, name, atype)
            if not paths:
                remove_asset(key)
                append_audit("remove", key, note="fetched 本体缺失，仅清理 registry")
                print(f"🗑  已清理 registry（fetched 本体缺失）: {key}")
                return 0
            trash = os.path.join(trash_dir(), f"fetched_{atype}_{name}")
            os.makedirs(trash, exist_ok=True)
            for p in paths:
                shutil.move(p, os.path.join(trash, os.path.basename(p)))
            remove_asset(key)
            append_audit("remove", key)
            print(f"🗑  已移入回收站: {_asset_display('fetched', atype, name)}")
            return 0

    # registry 无记录但 fetched 存在同名资产（孤儿，如 registry 被清但文件残留）
    # → 优先删 fetched 本体，不误伤 public/private 同名（_find_asset 优先级会误删 public）
    for t in ASSET_TYPES:
        if _asset_paths(source_dir("fetched", t), name, t):
            d = source_dir("fetched", t)
            paths = _asset_paths(d, name, t)
            trash = os.path.join(trash_dir(), f"fetched_{t}_{name}")
            os.makedirs(trash, exist_ok=True)
            for p in paths:
                shutil.move(p, os.path.join(trash, os.path.basename(p)))
            append_audit("remove", f"{t}/{name}", note="fetched 孤儿资产（无 registry）")
            print(f"🗑  已移入回收站（fetched 孤儿）: {_asset_display('fetched', t, name)}")
            return 0

    # 无 registry 或非 fetched → private/public 资产删除
    found = _find_asset(name)
    if not found:
        print(f"❌ 未找到资产: {name}")
        return 1
    source, atype = found
    d = source_dir(source, atype)
    paths = _asset_paths(d, name, atype)

    trash = os.path.join(trash_dir(), f"{source}_{atype}_{name}")
    os.makedirs(trash, exist_ok=True)
    for p in paths:
        shutil.move(p, os.path.join(trash, os.path.basename(p)))
    remove_asset(f"{atype}/{name}")
    append_audit("remove", f"{atype}/{name}")
    # 与 new/promote/demote 一致：private/public 资产自动提交 git（fetched 非仓库跳过）
    if source in ("private", "public"):
        root = source_root(source)
        ensure_repo(root)
        commit_all(root, f"remove {atype}: {name}")
    print(f"🗑  已移入回收站: {_asset_display(source, atype, name)}")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    """重新拉取并覆盖已安装的 fetched 资产（走审查门禁：高危阻断，无高危自动续审）。"""
    name = args.name
    found = _find_reg_entry_by_name(name)
    reg = found[1] if found else None
    if reg is None or not reg.get("source"):
        print(f"❌ 未找到可更新的 fetched 资产: {name}（需要 registry 中记录 source）")
        return 1
    key = cast(tuple[str, dict[str, Any]], found)[0]

    print(f"↻  重新拉取: {reg['source']}")
    rc, staging, _provenance, assets = _do_fetch(reg["source"], mode="update")
    if rc != 0:
        print(f"❌ 拉取失败: {reg['source']}")
        return 1

    # 新版本仍含该资产？（仓库可能已移除）
    if not any(a["name"] == name for a in assets):
        print(f"❌ 远程仓库中已不存在该资产: {name}")
        return 1

    # 门禁：新版本高危 → 阻断人工审查
    hits = scan_directory(cast(str, staging))
    high_hits = [h for file_hits in hits.values() for h in file_hits if getattr(h, "severity", "") == "HIGH"]
    if high_hits:
        print("❌ 新版本静态扫描发现高危风险，需人工审查：")
        print(format_report(hits))
        print(f'   fp ext review {name} --approve --note "复核通过"，然后 fp ext install {name}')
        return 1

    # 无高危 → 自动续审（来源信任延续，审计留痕）
    note = "update 自动续审（同 source 重新拉取，静态扫描无高危）"
    _mark_reviewed(key, note)
    print(f"✅ 自动续审通过: {key}（{note}）")

    # 落地覆盖（--force：已安装资产允许覆盖）
    args.force = True
    args.name = name
    rc = cmd_install(args)
    if rc == 0:
        append_audit("update", key, origin=reg["source"])
    return rc


def cmd_check(args: argparse.Namespace) -> int:
    """存量体检：unmanaged 资产 + 静态扫描 + 审计核对。"""
    print("── 存量体检 ──")
    reg = load_registry()
    issues = 0

    # 1. 无 manifest 的资产（unmanaged）
    unmanaged: list[str] = []
    for source in SOURCES:
        for atype in ASSET_TYPES:
            d = source_dir(source, atype)
            if not os.path.isdir(d):
                continue
            for fname in sorted(os.listdir(d)):
                if fname.startswith(".") or fname == "__pycache__" or fname.endswith(".disabled"):
                    continue
                fpath = os.path.join(d, fname)
                if os.path.isfile(fpath) and (
                    (fpath.endswith(".py") and parse_fp_manifest(fpath) is None)
                    or (fpath.endswith(".md") and parse_memory_manifest(fpath) is None)
                ):
                    unmanaged.append(f"{source}/{atype}/{fname}")
                elif os.path.isdir(fpath) and not any(
                    parse_fp_manifest(os.path.join(r, f))
                    for r, _d, fs in os.walk(fpath)
                    for f in fs
                    if f.endswith(".py")
                ):
                    # 目录型资产：内有 py 但无 manifest
                    unmanaged.append(f"{source}/{atype}/{fname}/")

    if unmanaged:
        print(f"⚠️  发现 {len(unmanaged)} 个未纳管资产（无 manifest）：")
        for u in unmanaged:
            print(f"   · {u}")
        print("   引导：fp ext init <dir> 补充 manifest")
        issues += len(unmanaged)
    else:
        print("✅ 所有资产均有 manifest")

    # 2. 待安装资产（pending_review / reviewed 未落地）
    pending = [
        (k, v)
        for k, v in reg["assets"].items()
        if v.get("status") in ("pending_review", "reviewed") and not v.get("installed_at")
    ]
    if pending:
        print(f"\n⏳  {len(pending)} 个待安装资产（已 fetch 未落地）：")
        for k, v in pending:
            status = v.get("status")
            if status == "pending_review":
                print(f"   · {k} [待审查] → 会话中审查后：fp ext review {v.get('name')} --approve")
            else:
                print(f"   · {k} [已审查] → fp ext install {v.get('name')}")
        issues += len(pending)

    # 3. fetched 资产静态扫描
    fetched_tools = source_dir("fetched", "tools")
    if os.path.isdir(fetched_tools):
        hits = scan_directory(fetched_tools)
        if hits:
            print("\n── fetched 资产静态扫描 ──")
            print(format_report(hits))
            issues += sum(len(v) for v in hits.values())

    # 3. 审计核对
    audit = load_audit(limit=5)
    print(f"\n── 审计日志（最近 {len(audit)} 条） ──")
    for a in audit:
        print(f"   {a['ts']} {a['action']} {a['asset']}")

    if issues == 0:
        print("\n✅ 体检通过")
    else:
        print(f"\n⚠️  发现 {issues} 处需处理")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    """生成新资产脚手架（到 private/）。"""
    atype = args.type
    name = args.name
    if atype not in ASSET_TYPES:
        print(f"❌ 类型必须是 {ASSET_TYPES} 之一")
        return 1

    dest_dir = source_dir("private", atype)
    os.makedirs(dest_dir, exist_ok=True)

    safe_name = name.replace(" ", "_").replace("/", "_")
    manifest_lines = (
        f"__fp__ = {{\n"
        f'    "name": "{safe_name}",\n'
        f'    "version": "0.1.0",\n'
        f'    "description": "",\n'
        f'    "license": "GPL-3.0",\n'
        f'    "type": "{atype}",\n'
        f"}}\n"
    )

    if atype == "tools":
        path = os.path.join(dest_dir, f"{safe_name}_plugin.py")
        if os.path.exists(path):
            print(f"❌ 已存在: {path}")
            return 1
        with open(path, "w", encoding="utf-8") as f:
            f.write(f'"""{safe_name} 工具插件"""\n\n')
            f.write(manifest_lines + "\n")
            f.write('PLUGIN_DEFINITION = {\n    "type": "function",\n    "function": {\n')
            f.write(f'        "name": "{safe_name}",\n')
            f.write('        "description": "",\n')
            f.write('        "parameters": {"type": "object", "properties": {}},\n')
            f.write("    },\n}\n\n\n")
            f.write('async def execute(params: dict) -> str:\n    """工具实现"""\n    return ""\n')
        print(f"✅ 已生成: {path}")
    elif atype == "commands":
        path = os.path.join(dest_dir, f"{safe_name}.py")
        if os.path.exists(path):
            print(f"❌ 已存在: {path}")
            return 1
        with open(path, "w", encoding="utf-8") as f:
            f.write(f'"""{safe_name} 命令"""\n\n')
            f.write(manifest_lines + "\n")
            f.write('name = "' + safe_name + '"\n')
            f.write("aliases = []\n")
            f.write('description = ""\n\n\n')
            f.write(
                "async def execute(state, arg: str):\n"
                '    """命令实现，返回 (handled, output)"""\n'
                '    return (True, "")\n'
            )
        print(f"✅ 已生成: {path}")
    elif atype == "plugins":
        pkg = os.path.join(dest_dir, safe_name)
        if os.path.isdir(pkg):
            print(f"❌ 已存在: {pkg}")
            return 1
        os.makedirs(pkg, exist_ok=True)
        with open(os.path.join(pkg, "__init__.py"), "w", encoding="utf-8") as f:
            f.write(manifest_lines + "\n")
            f.write("from fp_core.plugins.base.plugin import Plugin, PluginConfig\n\n\n")
            f.write(f"class {safe_name.title().replace('_', '')}Plugin(Plugin):\n")
            f.write(f'    name = "{safe_name}"\n')
            f.write("    def on_register(self, lifecycle):\n        pass\n\n")
            f.write("    def on_unregister(self):\n        pass\n")
        print(f"✅ 已生成: {pkg}")
    elif atype == "memory":
        path = os.path.join(dest_dir, f"{safe_name}.md")
        if os.path.exists(path):
            print(f"❌ 已存在: {path}")
            return 1
        with open(path, "w", encoding="utf-8") as f:
            f.write("---\n")
            f.write(f"name: {safe_name}\n")
            f.write("description: \n")
            f.write(f"type: {atype}\n")
            f.write("created: \n")
            f.write("---\n\n")
        print(f"✅ 已生成: {path}")

    # 自动纳入 private 仓库
    root = source_root("private")
    ensure_repo(root)
    commit_all(root, f"new {atype}: {safe_name}")
    append_audit("new", f"{atype}/{safe_name}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """为已有资产补充/校验 __fp__ manifest。"""
    target = os.path.abspath(args.dir)
    if not os.path.isdir(target):
        print(f"❌ 目录不存在: {target}")
        return 1

    # 找类型：按路径是否在三来源某类型目录下
    atype = None
    for t in ASSET_TYPES:
        if (
            target.startswith(source_dir("private", t))
            or target.startswith(source_dir("public", t))
            or target.startswith(source_dir("fetched", t))
        ):
            atype = t
            break
    if atype is None:
        print("❌ 无法推断资产类型（需位于三来源的某类型目录下）")
        return 1

    # 找主文件（目录型资产规则，与 _asset_filepath/_scan_public_assets 一致）
    main_file = _find_main_file(target, atype)
    if main_file is None:
        print(f"❌ 未找到 {atype} 主文件（{target}）")
        return 1

    # 已存在 manifest → 校验
    m = parse_fp_manifest(main_file) if main_file.endswith(".py") else parse_memory_manifest(main_file)
    if m:
        issues = validate_manifest(m, atype)
        if issues:
            print("⚠️  manifest 存在问题：")
            for i in issues:
                print(f"   · {i}")
            return 1
        print(f"✅ manifest 有效: {main_file}")
        return 0

    # 无 manifest → 追加（工具/命令：文件头；插件：__init__.py 头部；记忆：frontmatter）
    name = os.path.basename(target.rstrip("/")) or os.path.basename(os.path.dirname(target))
    if atype in ("tools", "commands", "memory") and main_file:
        base = os.path.basename(main_file)
        name = base[: -len(".py")] if base.endswith(".py") else base[: -len(".md")] if base.endswith(".md") else base
        if atype == "tools":
            name = name.replace("_plugin", "")
    if main_file.endswith(".py"):
        with open(main_file, encoding="utf-8") as f:
            content = f.read()
        block = (
            f"__fp__ = {{\n"
            f'    "name": "{name}",\n'
            f'    "version": "0.1.0",\n'
            f'    "description": "",\n'
            f'    "license": "GPL-3.0",\n'
            f'    "type": "{atype}",\n'
            f"}}\n"
        )
        # 插入到模块 docstring 闭合之后（正确识别多行 docstring）
        mdoc = re.match(r'^(\s*("""|\'\'\'))', content)
        if mdoc:
            quote = mdoc.group(2)
            end = content.find(quote, mdoc.end())
            if end != -1:
                insert_at = end + len(quote)
                content = content[:insert_at] + "\n\n" + block + "\n" + content[insert_at:]
            else:
                content = block + "\n" + content
        else:
            content = block + "\n" + content
        with open(main_file, "w", encoding="utf-8") as f:
            f.write(content)
    else:
        with open(main_file, encoding="utf-8") as f:
            content = f.read()
        block = f"---\nname: {name}\ndescription: \ntype: {atype}\ncreated: \n---\n\n"
        with open(main_file, "w", encoding="utf-8") as f:
            f.write(block + content.lstrip())

    print(f"✅ 已补充 manifest: {main_file}")
    append_audit("init", f"{atype}/{name}")
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    """私有 → 公开（移动资产到 public/ + 隐私扫描 + 登记清单 + 双仓 commit）。

    分享=移动：private 与 public 各保持单一版本，杜绝多重副本失同步。
    promote 把资产从 private/ 移入 public/（private 不再保留），
    public 即分享仓库，整个仓库可 share/push。
    """
    name = args.name
    found = _find_asset(name)
    if not found or found[0] != "private":
        print(f"❌ 资产不在 private/ 中: {name}（promote 仅对私有资产开放）")
        return 1
    _, atype = found
    src_dir = source_dir("private", atype)
    dest_dir = source_dir("public", atype)

    # 前置：public 库清单必须存在（移动模型下 promote 即"移动+登记"原子操作；
    # 清单缺失则资产移走后无登记成孤儿，且无法再 promote 补救——资产已不在 private）
    if _load_public_index() is None:
        print(f"❌ public 仓库缺少库级清单 {SHARE_INDEX}（位于 {source_root('public')}/）。")
        print("   移动前请先创建（声明来源与许可）：")
        print("   {")
        print('     "schema": 1,')
        print('     "author": "<你的名字>",')
        print('     "license": "GPL-3.0",')
        print('     "assets": {}')
        print("   }")
        return 1
    os.makedirs(dest_dir, exist_ok=True)

    # 定位资产路径
    paths = _asset_paths(src_dir, name, atype)
    if not paths:
        print(f"❌ 未找到 {name} 的实际文件")
        return 1

    # 隐私扫描（promote 场景）
    print("── 隐私扫描 ──")
    hits: dict[str, list[Hit]] = {}
    for p in paths:
        if os.path.isfile(p) and p.endswith(".py"):
            hs = scan_file(p, scene="promote")
            if hs:
                hits[os.path.basename(p)] = hs
    if hits:
        print(format_report(hits))
        if not args.force:
            print("\n❌ 检测到隐私风险，已中止。确认无泄露可 --force 强制。")
            return 1
        print("⚠️  --force：忽略隐私风险继续")

    # 移动资产到 public/（分享=移动；目标已存在则先移除旧版再移入，保证单版本）
    for p in paths:
        dest = os.path.join(dest_dir, os.path.basename(p))
        if os.path.exists(dest):
            if os.path.isdir(dest):
                shutil.rmtree(dest, ignore_errors=True)
            else:
                os.unlink(dest)
        shutil.move(p, dest)

    # 登记进 public 库清单（清单已在前置校验存在；登记必然发生，不留悬空资产）
    _register_asset_in_index(atype, name)

    # git：private 移出 + public 移入，双仓各自提交
    ensure_repo(source_root("private"))
    commit_all(source_root("private"), f"promote {atype}/{name}: moved to public")
    ensure_repo(source_root("public"))
    commit_all(source_root("public"), f"promote {atype}/{name}")

    append_audit("promote", f"{atype}/{name}")
    print(f"✅ 已公开（移动）: {_asset_display('public', atype, name)}")
    print(f"   private 已移出（单一版本，无副本）: {_asset_display('private', atype, name)}")
    print("   下一步：fp ext share 校验并发布 public 仓库")
    return 0


def cmd_demote(args: argparse.Namespace) -> int:
    """公开 → 私有（把资产从 public/ 移回 private/）。

    分享=移动：demote 是 promote 的逆操作——从 public 移回 private，
    同步移除库清单登记，双仓各自 commit。不产生副本，不留 .trash。
    注意：这里**只查 public**，不能走 _find_asset（默认优先 private）。
    """
    name = args.name
    atype = None
    for t in ASSET_TYPES:
        if _asset_filepath(source_dir("public", t), name, t):
            atype = t
            break
    if atype is None:
        print(f"❌ 资产不在 public/ 中: {name}（demote 仅对公开资产开放）")
        return 1
    src_dir = source_dir("public", atype)
    dest_dir = source_dir("private", atype)
    os.makedirs(dest_dir, exist_ok=True)

    paths = _asset_paths(src_dir, name, atype)
    if not paths:
        print(f"❌ 未找到 {name} 的实际文件")
        return 1

    # 移回 private（目标已存在同名 → 拒绝，防止覆盖用户当前工作区文件）
    for p in paths:
        dest = os.path.join(dest_dir, os.path.basename(p))
        if os.path.exists(dest):
            print(f"❌ private/ 已存在同名资产 {dest}，demote 会覆盖工作区文件。")
            print("   请先处理 private 中的同名资产（移动/删除）后重试。")
            return 1
        shutil.move(p, dest)

    # 同步移除库清单登记
    _unregister_asset_in_index(atype, name)

    # git：public 移出 + private 移入，双仓各自提交
    ensure_repo(source_root("public"))
    commit_all(source_root("public"), f"demote {atype}/{name}")
    ensure_repo(source_root("private"))
    commit_all(source_root("private"), f"demote {atype}/{name}: moved to private")

    append_audit("demote", f"{atype}/{name}")
    print(f"✅ 已收回（移动回 private）: {_asset_display('private', atype, name)}")
    print(f"   public 已移出（单一版本，无副本）: {_asset_display('public', atype, name)}")
    return 0


def cmd_share(args: argparse.Namespace) -> int:
    """发布 public/ 仓库（单仓库模型：public 即分享仓库）。

    职责：校验库清单 + 文件级 __fp__ + 孤儿文件 + 代码检查（静态扫描），
    通过后提交并（可选）推送。public 仓库是用户全部公开资产所在的唯一仓库。
    """
    public_root = source_root("public")
    if not os.path.isdir(public_root):
        print(f"❌ public 仓库不存在: {public_root}")
        return 1

    # 1. 库级清单 fp.ext.json（必须存在且含 schema=1，缺失拒绝、不自动创建）
    index = _load_public_index()
    if index is None:
        print(f"❌ public 仓库缺少有效库级清单 {SHARE_INDEX}（位于 {public_root}/）。")
        print("   请在 public 仓库根目录创建（仓库级元数据，声明来源与许可）：")
        print("   {")
        print('     "schema": 1,')
        print('     "author": "<你的名字>",')
        print('     "license": "GPL-3.0",')
        print('     "assets": {}')
        print("   }")
        return 1
    if index.get("schema") != 1:
        print(f"❌ 库级清单 {SHARE_INDEX} 无效（缺少 schema=1 协议版本声明）。")
        print("   请按仓库级元数据格式补齐：schema / author / license / assets")
        return 1
    if not index.get("author") or not index.get("license"):
        print(f"⚠️  库级清单 {SHARE_INDEX} 缺少 author 或 license（建议补齐后发布）。")
        return 1

    # 2. 枚举 public 资产 + 文件级 __fp__ 检查（分享物必须自描述）
    assets = _scan_public_assets()
    issues: list[str] = []
    for a in assets:
        if a["manifest"] is None:
            issues.append(f"资产缺少 __fp__ manifest（分享物必须自描述）: {a['atype']}/{os.path.basename(a['path'])}")
    if issues:
        print("❌ 以下资产未自描述，拒绝发布（请先 fp ext init 补充 manifest）：")
        for i in issues:
            print(f"   · {i}")
        return 1

    # 3. 孤儿文件检查（根目录杂散 / 清单悬空 / 未登记）
    orphan_issues = _check_public_orphans(assets, index)
    if orphan_issues:
        print("❌ public 仓库存在孤儿文件/登记不一致，拒绝发布：")
        for i in orphan_issues:
            print(f"   · {i}")
        return 1

    # 4. 代码检查：public 全资产静态扫描（install 规则 + promote 隐私规则）
    if getattr(args, "force", False):
        print("⚠️  --force：跳过静态扫描（清单/自描述/孤儿校验仍执行）")
    else:
        scan_hits: dict[str, list[Hit]] = {}
        for a in assets:
            if os.path.isfile(a["path"]) and a["path"].endswith(".py"):
                hs = scan_file(a["path"], scene="promote")
                if hs:
                    scan_hits[os.path.basename(a["path"])] = hs
        if scan_hits:
            print("❌ public 仓库存在静态扫描风险，拒绝发布（确认无泄露可 --force 强制）：")
            print(format_report(scan_hits))
            return 1

    # 5. 提交 + 可选推送
    ensure_repo(public_root)
    commit_all(public_root, "share: 发布 public 仓库")
    if args.push:
        if not has_remote(public_root):
            print("⚠️  public 仓库无 remote，跳过 push（先 git remote add origin <url>）")
        else:
            run_git(public_root, "push")
            print("🚀 已 push public 仓库")

    append_audit("share", "public", origin=public_root)
    print(f"✅ public 仓库已通过校验并发布: {public_root}")
    print("   他人安装：fp ext fetch <仓库 URL>")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    """手动执行存量迁移。"""
    report = migrate_once()
    if not report:
        print("✅ 无需迁移（老结构不存在）")
        return 0
    print("── 迁移报告 ──")
    for line in report:
        print(line)
    print("\n✅ 迁移完成")
    return 0


# ═══════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════


class _ExtParser(argparse.ArgumentParser):
    """参数错误时只输出文档引导（argparse 原始 error 与 usage 均为噪声：
    choices 列表重复出现，且 agent 顺着引导读文档即可获得全部信息）。"""

    def error(self, message: str) -> NoReturn:  # noqa: N802 — argparse 签名
        sys.stderr.write("fp ext: 参数错误。命令详情：fp ext <命令> -h\n")
        sys.stderr.write("完整文档：fp docs self/扩展分发.md    目录树：fp docs --list\n")
        sys.exit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = _ExtParser(
        prog="fp ext",
        description="扩展资产分发（管道工具：拉取→审查→落地→管理）",
        epilog="完整文档：fp docs self/扩展分发.md    目录树：fp docs --list",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("fetch", help="拉取资产到暂存区")
    p.add_argument("source", help="git URL / 本地路径 / 单文件 URL")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("install", help="从暂存区落地到 fetched/（须先 review --approve）")
    p.add_argument("name", help="暂存区资产名")
    p.add_argument("--source", default="", help="来源 URL（写入 registry）")
    p.add_argument("--force", action="store_true", help="覆盖已存在资产")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("review", help="记录审查结论（审计第二段）")
    p.add_argument("name", help="资产名")
    p.add_argument("--approve", action="store_true", help="通过审查")
    p.add_argument("--reject", action="store_true", help="拒绝安装")
    p.add_argument("--note", default="", help="审查意见（写入审计）")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("list", help="列出三来源所有资产")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("info", help="查看资产详情")
    p.add_argument("name")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("remove", help="删除资产（进回收站）")
    p.add_argument("name")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("update", help="重新拉取并覆盖 fetched 资产")
    p.add_argument("name")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("check", help="存量体检")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("new", help="生成新资产脚手架")
    p.add_argument("type", choices=ASSET_TYPES)
    p.add_argument("name")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("init", help="为资产补充/校验 manifest")
    p.add_argument("dir", help="资产目录")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("promote", help="私有 → 公开（移动资产到 public/，private 不留副本）")
    p.add_argument("name")
    p.add_argument("--force", action="store_true", help="忽略隐私扫描风险")
    p.add_argument("--push", action="store_true", help="推送 public 仓库")
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("demote", help="公开 → 私有（移回 private/，public 不留副本）")
    p.add_argument("name")
    p.set_defaults(func=cmd_demote)

    p = sub.add_parser("share", help="校验并发布 public 仓库（库清单/__fp__/孤儿/代码检查 + commit + 可选 push）")
    p.add_argument("--push", action="store_true", help="推送到 public 仓库 remote")
    p.add_argument("--force", action="store_true", help="跳过静态扫描拦截（仍执行清单/自描述/孤儿校验）")
    p.set_defaults(func=cmd_share)

    p = sub.add_parser("migrate", help="手动执行存量迁移")
    p.set_defaults(func=cmd_migrate)

    return parser


def ext_main(argv: list[str] | None = None) -> int:
    """fp ext 入口。返回进程退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # 空参 → 帮助 + 文档引导（而非 argparse 报错）
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except GitError as e:
        print(f"❌ {e}")
        return 1
    except KeyboardInterrupt:
        print("\n已取消")
        return 130
    except Exception as e:  # noqa: BLE001 — CLI 兜底
        print(f"❌ 执行失败: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(ext_main())
