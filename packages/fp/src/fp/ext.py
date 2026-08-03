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
  promote <name>     私有 → 公开（移动 + 隐私扫描 + git + 可选 push）
  demote <name>      公开 → 私有（收回）
  share <name>       发布通道：复制快照到分享仓库并 push
  migrate            手动执行存量迁移（老结构 → private/）
"""

import argparse
import hashlib
import json
import os
import shutil
import sys

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


# 暂存区（fetch 后的审查场所）——动态获取，避免模块级绑定导致路径固化（测试/换环境时残留）
def _staging_dir() -> str:
    return os.path.join(os.path.dirname(registry_path()), ".staging")


# 分享仓库索引文件名（share 更新用）
SHARE_INDEX = "fp.ext.json"


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


def _find_asset(name: str) -> tuple[str, str] | None:
    """在三来源中查找资产，返回 (来源, 类型)。优先级 private > public > fetched。"""
    for source in reversed(SOURCES):  # private 优先
        for atype in ASSET_TYPES:
            d = source_dir(source, atype)
            candidates = [name, f"{name}.py", f"{name}.md"]
            if atype == "tools":
                candidates.append(f"{name}_plugin.py")
            for c in candidates:
                if os.path.isfile(os.path.join(d, c)):
                    return source, atype
                if os.path.isdir(os.path.join(d, name)):
                    return source, atype
    return None


def _asset_display(source: str, atype: str, name: str) -> str:
    return f"{source}/{atype}/{name}"


# ═══════════════════════════════════════════════════════════════
# 命令实现
# ═══════════════════════════════════════════════════════════════


def _resolve_asset_identity(staging: str) -> tuple[str, str]:
    """从暂存区解析 (type, name)。优先 manifest，否则目录猜测。"""
    for root, _dirs, files in os.walk(staging):
        for fname in files:
            fpath = os.path.join(root, fname)
            if fname.endswith(".py"):
                m = parse_fp_manifest(fpath)
                if m and m.get("type") in ASSET_TYPES and m.get("name"):
                    return m["type"], m["name"]
            elif fname.endswith(".md"):
                m = parse_memory_manifest(fpath)
                if m and m.get("name"):
                    return "memory", m["name"]
    base = os.path.basename(staging.rstrip("/"))
    if base == "fetch_git":
        base = "unknown"
    has_md = any(f.endswith(".md") for _r, _d, fs in os.walk(staging) for f in fs)
    return ("memory" if has_md else "tools"), base


def _do_fetch(src: str) -> tuple[int, str | None, dict, str | None]:
    """核心拉取逻辑。

    返回 (rc, staging, provenance, entry_key)：
      rc        退出码（0 成功）
      staging   暂存区路径
      provenance 来源信息（type/source/commit/sha256）
      entry_key registry key（"type/name"），已登记 status=pending_review
    """
    staging: str | None = None
    provenance: dict = {}

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
        return 1, None, {}, None

    # 解析资产身份 + 静态扫描 → 登记 pending_review
    atype, aname = _resolve_asset_identity(staging)
    key = f"{atype}/{aname}"
    scan_report = scan_directory(staging)
    high_count = sum(1 for file_hits in scan_report.values() for h in file_hits if getattr(h, "severity", "") == "HIGH")
    reg_entry = {
        "name": aname,
        "type": atype,
        "source": provenance.get("source", ""),
        "commit": provenance.get("commit", ""),
        "sha256": provenance.get("sha256", ""),
        "fetched_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "staging": staging,
        "status": "pending_review",
        "scan_high": high_count,
    }
    upsert_asset(key, reg_entry)
    return 0, staging, provenance, key


def cmd_fetch(args) -> int:
    """拉取资产到暂存区 + 静态扫描 + 登记 pending_review。"""
    rc, staging, provenance, entry_key = _do_fetch(args.source)
    if rc != 0:
        print(f"❌ 无法识别的来源: {args.source}")
        return 1

    print(f"📥 已拉取到暂存区: {staging}")
    if provenance.get("commit"):
        print(f"   commit: {provenance['commit'][:12]}")
    if provenance.get("sha256"):
        print(f"   sha256: {provenance['sha256'][:16]}…")
    if entry_key:
        print(f"   登记: {entry_key} [pending_review]")

    # 解析清单 + 静态扫描
    print("\n── 资产清单 ──")
    found = 0
    for root, _dirs, files in os.walk(staging):
        for fname in files:
            fpath = os.path.join(root, fname)
            m = None
            if fname.endswith(".py"):
                m = parse_fp_manifest(fpath)
            elif fname.endswith(".md"):
                m = parse_memory_manifest(fpath)
            if m:
                found += 1
                print(f"  📦 {fname}: {m.get('name', '?')} v{m.get('version', '?')} — {m.get('description', '')}")
    if not found:
        print("  （未发现 __fp__ 协议字段，可能是非标准资产，仍可审查后手动 install）")

    print("\n── 静态扫描 ──")
    print(format_report(scan_directory(staging)))

    if entry_key:
        print("\n⏭  下一步（三阶段门禁）：在会话中审查暂存区源码，用户拍板后执行：")
        print(f'   fp ext review {entry_key.split("/", 1)[1]} --approve --note "审查意见"')
        print(f"   fp ext install {entry_key.split('/', 1)[1]}")
    else:
        print("\n⏭  下一步：审查暂存区源码后，执行 fp ext install <name> 落地。")
    return 0


def cmd_install(args) -> int:
    """从暂存区落地到 fetched/（门禁：须先 review --approve）。"""
    name = args.name
    staging = _staging_path(name)
    entry_key = None
    if not os.path.isdir(staging):
        # 从 registry pending 记录的 staging 找（fetch 登记的暂存区名可能 ≠ 资产名）
        for k, v in load_registry()["assets"].items():
            if v.get("name") == name and v.get("staging") and os.path.isdir(v["staging"]):
                staging = v["staging"]
                entry_key = k
                break
        if entry_key is None and os.path.isdir(_staging_path("fetch_git")):
            staging = _staging_path("fetch_git")
        if entry_key is None and not os.path.isdir(staging):
            print(f"❌ 暂存区不存在: {staging}（请先 fp ext fetch）")
            return 1

    # 确定资产类型（优先 registry 记录，其次 manifest，默认 tools）
    atype = "tools"
    manifest: dict | None = None
    if entry_key:
        atype = entry_key.split("/", 1)[0]
    else:
        for root, _dirs, files in os.walk(staging):
            for fname in files:
                if fname.endswith(".py"):
                    m = parse_fp_manifest(os.path.join(root, fname))
                    if m and m.get("type") in ASSET_TYPES:
                        atype = m["type"]
                        manifest = m
                        break
        if manifest is None and any(f.endswith(".md") for _r, _d, fs in os.walk(staging) for f in fs):
            # 目录猜测：有 .md → memory；有 execute+name → commands；否则 tools
            atype = "memory"

    # ── 三阶段门禁：审查状态校验 ──
    reg_entry = load_registry()["assets"].get(entry_key) if entry_key else None
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

    dest = source_dir("fetched", atype)
    os.makedirs(dest, exist_ok=True)
    dest_name = name if not name.startswith("fetch_git") else os.path.basename(staging)
    target = os.path.join(dest, dest_name)

    # 同名冲突
    if os.path.exists(target) and not args.force:
        print(f"⚠️  已存在: {target}")
        print("   使用 --force 覆盖（同 source 重新安装），或先 fp ext remove。")
        return 1

    # 复制（不移动——staging 保留到审查结束）
    if os.path.isdir(staging) and any(
        os.path.isfile(os.path.join(staging, f)) or os.path.isdir(os.path.join(staging, f)) for f in os.listdir(staging)
    ):
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(staging, target) if os.path.isdir(staging) else None
    else:
        shutil.copy2(staging, target)

    # 注册表（保留审查字段；无 registry 记录则构造新条目）
    now = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    if reg_entry:
        reg_entry["status"] = "active"
        reg_entry["installed_at"] = now
        upsert_asset(entry_key, reg_entry)
        source_origin = reg_entry.get("source", "")
    else:
        source_origin = args.source or ""
        reg_entry = {
            "name": dest_name,
            "type": atype,
            "source": source_origin,
            "commit": "",
            "sha256": "",
            "installed_at": now,
            "status": "active",
        }
        upsert_asset(f"{atype}/{dest_name}", reg_entry)
    append_audit("install", f"{atype}/{dest_name}", origin=source_origin)
    print(f"✅ 已安装: {_asset_display('fetched', atype, dest_name)}")
    print(f"   注册表: {atype}/{dest_name} [active]")
    return 0


def _find_reg_entry_by_name(name: str) -> tuple[str, dict] | None:
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


def cmd_review(args) -> int:
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


def cmd_list(args) -> int:
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
                if e.startswith(".") or e.endswith(".disabled"):
                    continue
                # 解析资产名（manifest name 优先，否则文件名）
                display = e
                fpath = os.path.join(d, e)
                if os.path.isfile(fpath):
                    if fpath.endswith(".py"):
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
            print(f"◉ {source}/")
            print("\n".join(lines))
    if total == 0:
        print("（空）")
    return 0


def cmd_info(args) -> int:
    """查看单个资产详情。"""
    name = args.name
    found = _find_asset(name)
    if not found:
        print(f"❌ 未找到资产: {name}")
        return 1
    source, atype = found
    d = source_dir(source, atype)
    fpath = None
    candidates = [name, f"{name}.py", f"{name}.md"]
    if atype == "tools":
        candidates.append(f"{name}_plugin.py")
    for c in candidates:
        if os.path.isfile(os.path.join(d, c)):
            fpath = os.path.join(d, c)
            break
    if fpath is None and os.path.isdir(os.path.join(d, name)):
        fpath = os.path.join(d, name)

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
    """定位资产的实际文件/目录路径（含 tools 命名约定 _plugin.py）。"""
    paths = []
    candidates = [name, f"{name}.py", f"{name}.md"]
    if atype == "tools":
        candidates.append(f"{name}_plugin.py")
    for c in candidates:
        p = os.path.join(d, c)
        if os.path.exists(p):
            paths.append(p)
    pkg = os.path.join(d, name)
    if os.path.isdir(pkg):
        paths.append(pkg)
    return paths


def cmd_remove(args) -> int:
    """删除资产（进 .trash 可恢复）。未安装的待审查资产直接清理 staging。"""
    name = args.name

    # 未安装的 pending/rejected 资产 → 清理 staging + registry
    found = _find_reg_entry_by_name(name)
    if found:
        key, entry = found
        if entry.get("status") in ("pending_review", "rejected") and not entry.get("installed_at"):
            staging = entry.get("staging")
            if staging and os.path.isdir(staging):
                shutil.rmtree(staging, ignore_errors=True)
            remove_asset(key)
            append_audit("remove", key, note="清理未安装的待审查资产")
            print(f"🗑  已清理待审查资产: {key}")
            return 0

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
    print(f"🗑  已移入回收站: {_asset_display(source, atype, name)}")
    return 0


def cmd_update(args) -> int:
    """重新拉取并覆盖已安装的 fetched 资产（走审查门禁：高危阻断，无高危自动续审）。"""
    name = args.name
    reg = None
    for atype in ASSET_TYPES:
        r = load_registry()["assets"].get(f"{atype}/{name}")
        if r:
            reg = r
            break
    if reg is None or not reg.get("source"):
        print(f"❌ 未找到可更新的 fetched 资产: {name}（需要 registry 中记录 source）")
        return 1

    print(f"↻  重新拉取: {reg['source']}")
    rc, staging, _provenance, key = _do_fetch(reg["source"])
    if rc != 0:
        print(f"❌ 拉取失败: {reg['source']}")
        return 1

    # 门禁：新版本高危 → 阻断人工审查
    hits = scan_directory(staging)
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

    # 落地覆盖
    args.force = True
    args.name = name
    rc = cmd_install(args)
    if rc == 0:
        append_audit("update", f"{reg['type']}/{name}", origin=reg["source"])
    return rc


def cmd_check(args) -> int:
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
                if fname.startswith(".") or fname.endswith(".disabled"):
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


def cmd_new(args) -> int:
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


def cmd_init(args) -> int:
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

    # 找主文件
    main_file = None
    if atype == "tools":
        for f in os.listdir(target):
            if f.endswith("_plugin.py"):
                main_file = os.path.join(target, f)
                break
    elif atype == "commands":
        for f in os.listdir(target):
            if f.endswith(".py") and not f.startswith("_"):
                main_file = os.path.join(target, f)
                break
    elif atype == "plugins":
        init = os.path.join(target, "__init__.py")
        if os.path.isfile(init):
            main_file = init
    else:
        for f in os.listdir(target):
            if f.endswith(".md"):
                main_file = os.path.join(target, f)
                break

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
            f'    "type": "{atype}",\n'
            f"}}\n"
        )
        # 插入到 docstring 之后
        if content.startswith('"""'):
            lines = content.split("\n", 2)
            content = lines[0] + "\n" + lines[1] + "\n\n" + block + "\n" + (lines[2] if len(lines) > 2 else "")
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


def cmd_promote(args) -> int:
    """私有 → 公开（移动 + 隐私扫描 + git + 可选 push）。"""
    name = args.name
    found = _find_asset(name)
    if not found or found[0] != "private":
        print(f"❌ 资产不在 private/ 中: {name}（promote 仅对私有资产开放）")
        return 1
    source, atype = found
    src_dir = source_dir("private", atype)
    dest_dir = source_dir("public", atype)
    os.makedirs(dest_dir, exist_ok=True)

    # 定位资产路径
    paths = _asset_paths(src_dir, name, atype)
    if not paths:
        print(f"❌ 未找到 {name} 的实际文件")
        return 1

    # 隐私扫描（promote 场景）
    print("── 隐私扫描 ──")
    hits = {}
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

    # 移动
    for p in paths:
        dest = os.path.join(dest_dir, os.path.basename(p))
        if os.path.exists(dest):
            print(f"❌ 目标已存在: {dest}（先 fp ext remove 或手动处理）")
            return 1
        shutil.move(p, dest)

    # git
    for src_root, msg in (
        (source_root("private"), f"promote {name} → public"),
        (source_root("public"), f"promote {name} ← private"),
    ):
        ensure_repo(src_root)
        commit_all(src_root, msg)
    if args.push and not has_remote(source_root("public")):
        print("⚠️  public 仓库无 remote，跳过 push（--repo 指定分享仓库见 fp ext share）")
    elif args.push:
        run_git(source_root("public"), "push")
        print("🚀 已 push public 仓库")

    append_audit("promote", f"{atype}/{name}")
    print(f"✅ 已公开: {_asset_display('public', atype, name)}")
    return 0


def cmd_demote(args) -> int:
    """公开 → 私有（收回，无隐私扫描）。"""
    name = args.name
    found = _find_asset(name)
    if not found or found[0] != "public":
        print(f"❌ 资产不在 public/ 中: {name}（demote 仅对公开资产开放）")
        return 1
    source, atype = found
    src_dir = source_dir("public", atype)
    dest_dir = source_dir("private", atype)
    os.makedirs(dest_dir, exist_ok=True)

    paths = _asset_paths(src_dir, name, atype)
    for p in paths:
        dest = os.path.join(dest_dir, os.path.basename(p))
        if os.path.exists(dest):
            print(f"❌ 目标已存在: {dest}")
            return 1
        shutil.move(p, dest)

    for src_root, msg in (
        (source_root("public"), f"demote {name} → private"),
        (source_root("private"), f"demote {name} ← public"),
    ):
        ensure_repo(src_root)
        commit_all(src_root, msg)

    append_audit("demote", f"{atype}/{name}")
    print(f"✅ 已收回: {_asset_display('private', atype, name)}")
    return 0


def cmd_share(args) -> int:
    """发布通道：复制快照到分享仓库并 push。"""
    name = args.name
    repo = args.repo
    if not repo:
        print("❌ 需要 --repo <dir> 指定分享仓库（用于推送的 git 仓库）")
        return 1
    repo = os.path.abspath(repo)
    if not os.path.isdir(repo):
        print(f"❌ 分享仓库不存在: {repo}")
        return 1

    found = _find_asset(name)
    if not found:
        print(f"❌ 未找到资产: {name}")
        return 1
    source, atype = found
    if source == "fetched":
        print("❌ fetched 资产禁止再分发（来源非原创，防套娃）")
        return 1

    src_dir = source_dir(source, atype)
    paths = _asset_paths(src_dir, name, atype)

    # 复制快照到分享仓库对应类型目录
    dest_dir = os.path.join(repo, atype)
    os.makedirs(dest_dir, exist_ok=True)
    for p in paths:
        dest = os.path.join(dest_dir, os.path.basename(p))
        if os.path.isdir(p):
            shutil.copytree(p, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(p, dest)

    # 更新索引 fp.ext.json
    index_path = os.path.join(repo, SHARE_INDEX)
    index = {}
    if os.path.isfile(index_path):
        try:
            with open(index_path, encoding="utf-8") as f:
                index = json.load(f)
        except (json.JSONDecodeError, OSError):
            index = {}
    index.setdefault("assets", {})
    index["assets"][f"{atype}/{name}"] = {
        "source": os.path.basename(name),
        "updated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)

    ensure_repo(repo)
    commit_all(repo, f"share {atype}/{name}")
    if args.push:
        run_git(repo, "push")
        print("🚀 已 push 分享仓库")
    append_audit("share", f"{atype}/{name}", origin=repo)
    print(f"✅ 已发布到分享仓库: {repo}")
    return 0


def cmd_migrate(args) -> int:
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fp ext",
        description="扩展资产分发（管道工具：拉取→审查→落地→管理）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

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

    p = sub.add_parser("promote", help="私有 → 公开")
    p.add_argument("name")
    p.add_argument("--force", action="store_true", help="忽略隐私扫描风险")
    p.add_argument("--push", action="store_true", help="推送 public 仓库")
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("demote", help="公开 → 私有")
    p.add_argument("name")
    p.set_defaults(func=cmd_demote)

    p = sub.add_parser("share", help="发布到分享仓库")
    p.add_argument("name")
    p.add_argument("--repo", required=True, help="分享仓库目录")
    p.add_argument("--push", action="store_true", help="推送分享仓库")
    p.set_defaults(func=cmd_share)

    p = sub.add_parser("migrate", help="手动执行存量迁移")
    p.set_defaults(func=cmd_migrate)

    return parser


def ext_main(argv: list[str] | None = None) -> int:
    """fp ext 入口。返回进程退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)
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
