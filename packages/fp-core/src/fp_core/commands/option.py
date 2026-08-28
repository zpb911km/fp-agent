"""
/option 命令 — 统一拓展管理

观测和管理三种拓展机制（commands / plugins / tools）。
纯用户命令，不修改 core，不搞热重载，不做 import 副作用。

用法:
  /option                    → 等同 /option list
  /option list [类型]        → 列出全部/指定类型拓展
  /option info <名>          → 查看拓展详情
  /option enable <名>        → 启用（重命名 .disabled → .py）
  /option disable <名>       → 禁用（重命名 .py → .disabled）
  /option diff <名>          → 内置 vs 用户版本差异对比
"""

import ast
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypedDict, cast

import fp_core
from fp_core.platform_utils import get_data_dir

# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════

FP_CORE_DIR = os.path.dirname(fp_core.__file__)
DATA_DIR = get_data_dir()

CORE_TOOLS = {"bash", "read_file", "write_file", "edit_file"}
PLUGIN_SKIP = {"base.py", "setup.py"}

# 三来源（资产分发系统）：fetched(外来/只读) → public(公开) → private(私有)
# 加载优先级：private > public > fetched；操作（enable/disable/diff）优先命中高优先级版本
_SOURCE_LABELS = {
    "builtin": "内置",
    "user": "用户",
    "fetched": "外来 (fetched)",
    "public": "公开 (public)",
    "private": "私有 (private)",
}


# 常量（供 disabled 文件名解析）
# 注意：".py.disabled" 长度为 12，勿用 11（曾导致名称多带一个点号）
_DISABLED_SUFFIX = ".py.disabled"
_DISABLED_SUFFIX_LEN = len(_DISABLED_SUFFIX)


def _user_dirs(kind: str, reverse: bool = False) -> list[str]:
    """三来源用户目录（fetched → public → private），仅返回已存在的目录。

    reverse=True 时返回 private → public → fetched（高优先级优先，用于操作与展示去重）。
    """
    from fp_core.config import user_dirs

    dirs = [d for d in user_dirs(kind) if os.path.isdir(d)]
    return list(reversed(dirs)) if reverse else dirs


name = "option"
aliases = ["opt", "op", "ext", "extension", "extensions"]
description = "统一管理三种拓展机制（commands / plugins / tools）"


# ═══════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════


class EntryContract(TypedDict, total=False):
    """Entry.contract 载荷（键随拓展 type 变化，command/plugin/tool 各取所需）"""

    aliases: list[str]
    version: str
    hooks: list[dict[str, Any]]
    class_name: str
    schema: dict[str, Any]
    core: bool


@dataclass
class Entry:
    """统一拓展条目"""

    name: str
    type: str  # "command" | "plugin" | "tool"
    description: str
    status: str  # "enabled" | "disabled"
    source: str  # "builtin" | "user"
    source_path: str
    user_override: bool = False
    contract: EntryContract = field(default_factory=EntryContract)


# ═══════════════════════════════════════════════════════════════
# OptionManager — 扫描+操作
# ═══════════════════════════════════════════════════════════════


class OptionManager:
    """封装三个扫描逻辑的差异，对外提供统一接口"""

    def __init__(self, state: Any):
        # state 为动态对象（插件/工具/生命周期注册表内部结构），用 Any 兜底
        self.state = state

    # ── 全量扫描 ──────────────────────────────────────────

    def scan_all(self) -> list[Entry]:
        return self.scan_commands() + self.scan_plugins() + self.scan_tools()

    # ── 命令扫描 ──────────────────────────────────────────

    def scan_commands(self) -> list[Entry]:
        import fp_core.commands as _cmds

        cmd_reg: dict[str, Any] = cast(dict[str, Any], _cmds._commands)

        # mod id 去重（别名指向同一模块）
        mod_map: dict[int, dict[str, Any]] = {}
        for name, mod in cmd_reg.items():
            mid = id(mod)
            if mid not in mod_map:
                mod_map[mid] = {"names": [], "aliases": [], "mod": mod, "file": getattr(mod, "__file__", "") or ""}
            entry = mod_map[mid]
            if hasattr(mod, "name") and name == mod.name:
                entry["names"].append(name)
            else:
                entry["aliases"].append(name)

        results: list[Entry] = []
        for _mid, info in mod_map.items():
            mod = info["mod"]
            cname = info["names"][0] if info["names"] else info["aliases"][0]
            desc = getattr(mod, "description", "")
            src_path = info["file"]
            source = self._classify_source(src_path)
            user_override = self._has_cmd_override(cname)

            results.append(
                Entry(
                    name=cname,
                    type="command",
                    description=desc,
                    status="enabled",
                    source=source,
                    source_path=src_path,
                    user_override=user_override,
                    contract={"aliases": info["aliases"]},
                )
            )

        # 用户目录中禁用的命令（三来源，优先级高→低，去重）
        seen: set[str] = set()
        for d in _user_dirs("commands", reverse=True):
            self._scan_disabled_cmds(d, results, mod_map, seen)
        return results

    def _has_cmd_override(self, name: str) -> bool:
        """命令是否有用户覆盖 — 内置和用户版本同时存在才算覆盖"""
        builtin_path = os.path.join(FP_CORE_DIR, "commands", f"{name}.py")
        if not os.path.isfile(builtin_path):
            return False
        for d in _user_dirs("commands"):
            user_path = os.path.join(d, f"{name}.py")
            if os.path.isfile(user_path) or os.path.isfile(user_path + ".disabled"):
                return True
        return False

    def _scan_disabled_cmds(
        self, directory: str, results: list[Entry], mod_map: dict[int, dict[str, Any]], seen: set[str]
    ) -> None:
        if not os.path.isdir(directory):
            return
        for fn in os.listdir(directory):
            if not fn.endswith(".py.disabled"):
                continue
            base = fn[:-_DISABLED_SUFFIX_LEN]  # strip .py.disabled（12 字符）
            if base == "__init__" or base in seen:
                continue
            # 检查是否已在注册表中
            found = False
            for _mid, info in mod_map.items():
                mod = info["mod"]
                if hasattr(mod, "name") and mod.name == base:
                    found = True
                    break
            if not found:
                seen.add(base)
                results.append(
                    Entry(
                        name=base,
                        type="command",
                        description="",
                        status="disabled",
                        source="user",
                        source_path=os.path.join(directory, fn),
                    )
                )

    # ── 插件扫描 ──────────────────────────────────────────

    def scan_plugins(self) -> list[Entry]:
        results: list[Entry] = []
        seen: set[str] = set()

        for pname in self.state.plugins.list_plugins():
            plugin = self.state.plugins.get(pname)
            if plugin is None:
                continue
            seen.add(pname)
            try:
                import inspect

                src_path = inspect.getfile(cast(Any, type(plugin)))
            except Exception:
                src_path = ""
            source = self._classify_source(src_path)
            hooks = self._get_plugin_hooks(pname)
            results.append(
                Entry(
                    name=pname,
                    type="plugin",
                    description=getattr(plugin, "description", ""),
                    status="enabled",
                    source=source,
                    source_path=src_path,
                    contract={
                        "version": getattr(plugin, "version", "?"),
                        "hooks": hooks,
                        "class_name": type(plugin).__name__,
                    },
                )
            )

        # 用户目录中禁用的插件（文件型 + 目录型，三来源，优先级高→低，共享 seen 去重）
        for d in _user_dirs("plugins", reverse=True):
            self._scan_disabled_files(
                d,
                results,
                seen,
                lambda f: not f.startswith("_") and f not in PLUGIN_SKIP,
                self._read_plugin_name_from_file,
            )
            self._scan_disabled_dirs(d, results, seen)

        # 内置目录中子目录（包插件），检查是否未加载（同名用户版禁用导致内置版也被屏蔽）
        bdir = os.path.join(FP_CORE_DIR, "plugins")
        if os.path.isdir(bdir):
            for entry in os.scandir(bdir):
                if not entry.is_dir() or entry.name.startswith("_") or entry.name.startswith("."):
                    continue
                if entry.name == "base":
                    continue
                init_path = os.path.join(entry.path, "__init__.py")
                if not os.path.isfile(init_path):
                    continue
                if entry.name not in seen:
                    desc = self._read_plugin_desc(entry.path)
                    results.append(
                        Entry(
                            name=entry.name,
                            type="plugin",
                            description=desc,
                            status="disabled",
                            source="builtin",
                            source_path=os.path.join(entry.path, "plugin.py"),
                            contract={"version": "?", "hooks": [], "class_name": "?"},
                        )
                    )
        return results

    def _read_plugin_desc(self, dirpath: str) -> str:
        for fn in ("plugin.py", "__init__.py"):
            fp = os.path.join(dirpath, fn)
            if os.path.isfile(fp):
                try:
                    with open(fp, encoding="utf-8") as f:
                        tree = ast.parse(f.read())
                    for node in ast.iter_child_nodes(tree):
                        if isinstance(node, ast.ClassDef):
                            for item in node.body:
                                if isinstance(item, ast.Assign):
                                    for t in item.targets:
                                        if (
                                            isinstance(t, ast.Name)
                                            and t.id == "description"
                                            and isinstance(item.value, ast.Constant)
                                        ):
                                            return str(item.value.value)
                except Exception:
                    pass
        return ""

    def _scan_disabled_files(
        self,
        directory: str,
        results: list[Entry],
        seen: set[str],
        filter_fn: Callable[[str], bool],
        name_reader: Callable[[str, str], str],
    ) -> None:
        if not os.path.isdir(directory):
            return
        for fn in os.listdir(directory):
            if not fn.endswith(".py.disabled"):
                continue
            base = fn[:-_DISABLED_SUFFIX_LEN]
            if not filter_fn(f"{base}.py"):
                continue
            fp = os.path.join(directory, fn)
            pname = name_reader(fp, base)
            if pname in seen:
                continue
            seen.add(pname)
            results.append(
                Entry(
                    name=pname,
                    type="plugin",
                    description="",
                    status="disabled",
                    source=self._classify_source(fp),
                    source_path=fp,
                    contract={"version": "?", "hooks": [], "class_name": "?"},
                )
            )

    def _scan_disabled_dirs(self, directory: str, results: list[Entry], seen: set[str]) -> None:
        """扫描目录型插件的禁用态（name.disabled/）"""
        if not os.path.isdir(directory):
            return
        for fn in os.listdir(directory):
            if not fn.endswith(".disabled"):
                continue
            dir_path = os.path.join(directory, fn)
            if not os.path.isdir(dir_path):
                continue
            base = fn[:-9]  # strip ".disabled"
            if base.startswith("_") or base in PLUGIN_SKIP:
                continue
            if not os.path.isfile(os.path.join(dir_path, "__init__.py")):
                continue
            if base in seen:
                continue
            pfile = os.path.join(dir_path, "plugin.py")
            pname = self._read_plugin_name_from_file(pfile, base) if os.path.isfile(pfile) else base
            seen.add(pname)
            results.append(
                Entry(
                    name=pname,
                    type="plugin",
                    description="",
                    status="disabled",
                    source=self._classify_source(dir_path),
                    source_path=dir_path,
                    contract={"version": "?", "hooks": [], "class_name": "?"},
                )
            )

    def _read_plugin_name_from_file(self, filepath: str, fallback: str) -> str:
        try:
            with open(filepath, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.ClassDef):
                    for item in node.body:
                        if isinstance(item, ast.Assign):
                            for t in item.targets:
                                if isinstance(t, ast.Name) and t.id == "name" and isinstance(item.value, ast.Constant):
                                    return str(item.value.value)
        except Exception:
            pass
        return fallback

    # ── 工具扫描 ──────────────────────────────────────────

    def scan_tools(self) -> list[Entry]:
        results: list[Entry] = []
        seen: set[str] = set()
        reg = self.state.tool_exec.registry

        # 核心工具
        for d in reg._core_defs:
            tname = d["function"]["name"]
            seen.add(tname)
            desc = d["function"]["description"]
            results.append(
                Entry(
                    name=tname,
                    type="tool",
                    description=desc.split(".")[0] if desc else "",
                    status="enabled",
                    source="builtin",
                    source_path="[core]",
                    contract={"schema": d["function"], "core": True},
                )
            )

        # 插件工具
        for _pkey, pdata in reg._plugins.items():
            defn = pdata["definition"]
            tname = defn["function"]["name"]
            seen.add(tname)
            desc = defn["function"]["description"]
            plugin_src = pdata.get("source", "")

            # 找源文件
            src_path = self._find_tool_file(plugin_src)
            source = self._classify_source(src_path) if src_path else "builtin"

            # 用户覆盖：当前是用户版且内置有同名 → True
            user_override = False
            if source == "user" and src_path:
                bdir = os.path.join(FP_CORE_DIR, "tools", "extensions")
                bname = os.path.basename(src_path)
                if os.path.isfile(os.path.join(bdir, bname)):
                    user_override = True
            elif source == "builtin":
                # 当前是内置版，检查用户目录是否有同名（三来源）
                for udir in _user_dirs("tools"):
                    for fn in os.listdir(udir):
                        clean = fn.replace(".disabled", "")
                        if clean.endswith("_plugin.py") and clean == f"{plugin_src}.py":
                            user_override = True
                            break

            results.append(
                Entry(
                    name=tname,
                    type="tool",
                    description=desc.split(".")[0] if desc else "",
                    status="enabled",
                    source=source,
                    source_path=src_path or f"[plugin:{plugin_src}]",
                    user_override=user_override,
                    contract={"schema": defn["function"], "core": False},
                )
            )

        # 用户/内置目录中禁用的工具（三来源，优先级高→低 + 内置兜底）
        for d in _user_dirs("tools", reverse=True):
            self._scan_disabled_tools(d, results, seen)
        self._scan_disabled_tools(os.path.join(FP_CORE_DIR, "tools", "extensions"), results, seen)

        return results

    def _find_tool_file(self, plugin_src: str) -> str:
        """找工具插件源文件（用户三来源优先，private 优先）"""
        candidates = [
            *(os.path.join(d, f"{plugin_src}.py") for d in _user_dirs("tools", reverse=True)),
            os.path.join(FP_CORE_DIR, "tools", "extensions", f"{plugin_src}.py"),
        ]
        for p in candidates:
            if os.path.isfile(p):
                return p
        return ""

    def _scan_disabled_tools(self, directory: str, results: list[Entry], seen: set[str]) -> None:
        if not os.path.isdir(directory):
            return
        for fn in os.listdir(directory):
            if not fn.endswith("_plugin.py.disabled"):
                continue
            fp = os.path.join(directory, fn)
            tname = self._read_tool_name(fp)
            if tname and tname not in seen:
                seen.add(tname)
                results.append(
                    Entry(
                        name=tname,
                        type="tool",
                        description="",
                        status="disabled",
                        source=self._classify_source(fp),
                        source_path=fp,
                        contract={"schema": {"name": tname}, "core": False},
                    )
                )

    def _read_tool_name(self, filepath: str) -> str:
        try:
            with open(filepath, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            if t.id == "PLUGIN_DEFINITION" and isinstance(node.value, ast.Dict):
                                for k, v in zip(node.value.keys, node.value.values, strict=False):
                                    if (
                                        isinstance(k, ast.Constant)
                                        and k.value == "function"
                                        and isinstance(v, ast.Dict)
                                    ):
                                        for fk, fv in zip(v.keys, v.values, strict=False):
                                            if (
                                                isinstance(fk, ast.Constant)
                                                and fk.value == "name"
                                                and isinstance(fv, ast.Constant)
                                            ):
                                                return str(fv.value)
                            if t.id == "PLUGIN_DEFINITIONS" and isinstance(node.value, ast.List):
                                for item in node.value.elts:
                                    if isinstance(item, ast.Dict):
                                        for k, v in zip(item.keys, item.values, strict=False):
                                            if (
                                                isinstance(k, ast.Constant)
                                                and k.value == "function"
                                                and isinstance(v, ast.Dict)
                                            ):
                                                for fk, fv in zip(v.keys, v.values, strict=False):
                                                    if (
                                                        isinstance(fk, ast.Constant)
                                                        and fk.value == "name"
                                                        and isinstance(fv, ast.Constant)
                                                    ):
                                                        return str(fv.value)
        except Exception:
            pass
        return ""

    # ── 通用 ──────────────────────────────────────────────

    def _classify_source(self, filepath: str) -> str:
        if not filepath:
            return "builtin"
        norm = os.path.normpath(filepath)
        if FP_CORE_DIR in norm:
            return "builtin"
        # 三来源（fetched/public/private），不在其中的归 user
        data = os.path.normpath(DATA_DIR)
        for s in ("fetched", "public", "private"):
            if norm.startswith(os.path.join(data, s)):
                return s
        return "user"

    def _is_fetched(self, path: str) -> bool:
        """外来资产（fetched/）只读，不可 enable/disable"""
        norm = os.path.normpath(path)
        return norm.startswith(os.path.normpath(os.path.join(DATA_DIR, "fetched")))

    def _get_plugin_hooks(self, plugin_name: str) -> list[dict[str, Any]]:
        lifecycle = getattr(self.state, "lifecycle", None)
        if lifecycle is None:
            return []
        hooks: list[dict[str, Any]] = []
        for hook_key, entries in lifecycle._hooks.items():
            for prio, reg_name, _func, htype in entries:
                if reg_name.startswith(plugin_name):
                    hooks.append({"hook": hook_key, "priority": prio, "name": reg_name, "type": htype})
        return hooks

    # ── 查找 ──────────────────────────────────────────────

    def get(self, name: str) -> Entry | None:
        items = self.scan_all()
        for item in items:
            if item.name == name:
                return item
        for item in items:
            if item.type == "command":
                for alias in item.contract.get("aliases", []):
                    if alias == name:
                        return item
        return None

    def get_by_type(self, name: str, etype: str) -> Entry | None:
        for item in self.scan_all():
            if item.name == name and item.type == etype:
                return item
        return None

    # ── 启用/禁用 ─────────────────────────────────────────

    def _find_disabled(self, name: str) -> str | None:
        """查找 name 对应的 .py.disabled 文件（三来源，private 优先）"""
        # 工具目录
        for d in _user_dirs("tools", reverse=True):
            for fn in os.listdir(d):
                if fn.endswith("_plugin.py.disabled"):
                    tname = self._read_tool_name(os.path.join(d, fn))
                    if tname == name:
                        return os.path.join(d, fn)
        # 插件目录
        for pd in _user_dirs("plugins", reverse=True):
            # 文件型插件：name.py.disabled
            for fn in os.listdir(pd):
                if fn.endswith(".py.disabled") and not fn.startswith("_") and fn not in PLUGIN_SKIP:
                    pname = self._read_plugin_name_from_file(os.path.join(pd, fn), fn[:-_DISABLED_SUFFIX_LEN])
                    if pname == name:
                        return os.path.join(pd, fn)
            # 目录型插件：name.disabled/
            disabled_dir = os.path.join(pd, f"{name}.disabled")
            if os.path.isdir(disabled_dir):
                init_path = os.path.join(disabled_dir, "__init__.py")
                if os.path.isfile(init_path):
                    return disabled_dir
        # 命令目录
        for cd in _user_dirs("commands", reverse=True):
            for fn in os.listdir(cd):
                if fn == f"{name}.py.disabled":
                    return os.path.join(cd, fn)
        return None

    def _find_enabled(self, name: str) -> str | None:
        """查找 name 对应的 .py 文件（仅用户三来源，private 优先）"""
        for d in _user_dirs("tools", reverse=True):
            for fn in os.listdir(d):
                if fn.endswith("_plugin.py"):
                    tname = self._read_tool_name(os.path.join(d, fn))
                    if tname == name:
                        return os.path.join(d, fn)
        for pd in _user_dirs("plugins", reverse=True):
            # 文件型插件
            for fn in os.listdir(pd):
                if fn.endswith(".py") and not fn.startswith("_") and fn not in PLUGIN_SKIP:
                    pname = self._read_plugin_name_from_file(os.path.join(pd, fn), fn[:-3])
                    if pname == name:
                        return os.path.join(pd, fn)
            # 目录型插件
            for fn in os.listdir(pd):
                entry_path = os.path.join(pd, fn)
                if not os.path.isdir(entry_path) or fn.startswith("_"):
                    continue
                if not os.path.isfile(os.path.join(entry_path, "__init__.py")):
                    continue
                pfile = os.path.join(entry_path, "plugin.py")
                if not os.path.isfile(pfile):
                    continue
                pname = self._read_plugin_name_from_file(pfile, fn)
                if pname == name:
                    return entry_path
        # 命令目录
        for cd in _user_dirs("commands", reverse=True):
            for fn in os.listdir(cd):
                if fn == f"{name}.py":
                    return os.path.join(cd, fn)
        return None

    def enable(self, name: str) -> tuple[bool, str]:
        disabled_path = self._find_disabled(name)
        if disabled_path is None:
            return (False, f"⚠️ 未找到已禁用的 '{name}'")
        if self._is_fetched(disabled_path):
            return (False, f"❌ 外来资产 (fetched) 只读，无法启用 '{name}'")
        is_dir = os.path.isdir(disabled_path)
        enabled_path = disabled_path[:-9]  # strip .disabled
        try:
            os.rename(disabled_path, enabled_path)
            kind = "目录" if is_dir else "文件"
            return (
                True,
                f"✅ 已启用 {name}\n\n{kind}: {disabled_path}\n\n→ {os.path.basename(enabled_path)}\n\n⚠️ 重启后生效",
            )
        except OSError as e:
            return (False, f"❌ 启用失败: {e}")

    def disable(self, name: str) -> tuple[bool, str]:
        if name in CORE_TOOLS:
            return (False, f"❌ 核心工具 '{name}' 不可禁用")
        if self._find_disabled(name) is not None:
            return (False, f"⚠️ '{name}' 已经是禁用状态")
        enabled_path = self._find_enabled(name)
        if enabled_path is None:
            return (False, f"⚠️ 未找到已启用的 '{name}'")
        if FP_CORE_DIR in os.path.normpath(enabled_path):
            return (False, "❌ 无法禁用内置拓展。如需覆盖，请在用户目录创建同名文件后再禁用")
        if self._is_fetched(enabled_path):
            return (False, f"❌ 外来资产 (fetched) 只读，无法禁用 '{name}'")
        is_dir = os.path.isdir(enabled_path)
        disabled_path = enabled_path + ".disabled"
        if os.path.exists(disabled_path):
            return (False, f"❌ 目标路径已存在: {disabled_path}")
        try:
            os.rename(enabled_path, disabled_path)
            kind = "目录" if is_dir else "文件"
            return (
                True,
                f"⛔ 已禁用 {name}\n\n{kind}: {enabled_path}\n\n→ {os.path.basename(disabled_path)}\n\n⚠️ 重启后生效",
            )
        except OSError as e:
            return (False, f"❌ 禁用失败: {e}")

    # ── 差异对比 ─────────────────────────────────────────

    def diff(self, name: str) -> dict[str, Any] | None:
        builtin_path = None
        user_path = None

        # 命令
        for d in (os.path.join(FP_CORE_DIR, "commands"), *_user_dirs("commands", reverse=True)):
            if os.path.isdir(d):
                for fn in os.listdir(d):
                    clean = fn.replace(".disabled", "")
                    if clean == f"{name}.py":
                        fp = os.path.join(d, fn)
                        if os.path.isfile(fp):
                            if FP_CORE_DIR in os.path.normpath(fp):
                                builtin_path = fp
                            else:
                                user_path = fp

        # 插件
        for d in (os.path.join(FP_CORE_DIR, "plugins"), *_user_dirs("plugins", reverse=True)):
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                fp = os.path.join(d, fn)
                if os.path.isdir(fp) and fn == name:
                    pfile = os.path.join(fp, "plugin.py")
                    if os.path.isfile(pfile):
                        if FP_CORE_DIR in os.path.normpath(fp):
                            builtin_path = pfile
                        else:
                            user_path = pfile
                else:
                    clean = fn.replace(".disabled", "").replace(".py", "")
                    if clean and not clean.startswith("_"):
                        pname = self._read_plugin_name_from_file(fp, clean)
                        if pname == name:
                            if FP_CORE_DIR in os.path.normpath(fp):
                                builtin_path = fp
                            else:
                                user_path = fp

        # 工具
        for d in (os.path.join(FP_CORE_DIR, "tools", "extensions"), *_user_dirs("tools", reverse=True)):
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                clean = fn.replace(".disabled", "")
                if clean.endswith("_plugin.py"):
                    fp = os.path.join(d, fn)
                    tname = self._read_tool_name(fp)
                    if tname == name:
                        if FP_CORE_DIR in os.path.normpath(fp):
                            builtin_path = fp
                        else:
                            user_path = fp

        if builtin_path is None and user_path is None:
            return None
        if builtin_path is None:
            return {"note": f"'{name}' 只有用户版本，无内置版本，无法对比"}
        if user_path is None:
            return {"note": f"'{name}' 只有内置版本，无用户覆盖，无需对比"}

        result = {
            "name": name,
            "builtin_path": builtin_path,
            "user_path": user_path,
            "builtin_lines": self._count_lines(builtin_path),
            "user_lines": self._count_lines(user_path),
        }
        b_schema = self._read_tool_schema(builtin_path)
        u_schema = self._read_tool_schema(user_path)
        if b_schema and u_schema:
            result["schema_match"] = b_schema == u_schema
        return result

    def _count_lines(self, fp: str) -> int:
        if not fp or not os.path.isfile(fp) or fp.startswith("["):
            return 0
        try:
            with open(fp, encoding="utf-8") as f:
                return sum(1 for _ in f)
        except Exception:
            return 0

    def _read_tool_schema(self, fp: str) -> dict[str, Any] | None:
        try:
            with open(fp, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Name) and t.id == "PLUGIN_DEFINITION" and isinstance(node.value, ast.Dict):
                            for k, v in zip(node.value.keys, node.value.values, strict=False):
                                if isinstance(k, ast.Constant) and k.value == "function" and isinstance(v, ast.Dict):
                                    for fk, fv in zip(v.keys, v.values, strict=False):
                                        if isinstance(fk, ast.Constant) and fk.value == "parameters":
                                            return ast.literal_eval(fv)
        except Exception:
            pass
        return None


# ═══════════════════════════════════════════════════════════════
# 辅助：按展示顺序获取有序列表（编号依据）
# ═══════════════════════════════════════════════════════════════


def _get_ordered_items(mgr: OptionManager, type_filter: str = "all", status_filter: str = "all") -> list[Entry]:
    """按展示顺序（命令→插件→工具，每组内已启用在前，按字母排序）"""
    items = mgr.scan_all()
    if type_filter != "all":
        items = [i for i in items if i.type == type_filter]
    if status_filter == "enabled":
        items = [i for i in items if i.status == "enabled"]
    elif status_filter == "disabled":
        items = [i for i in items if i.status == "disabled"]

    groups: dict[str, list[Entry]] = {"command": [], "plugin": [], "tool": []}
    for item in items:
        groups.setdefault(item.type, []).append(item)
    for t in groups:
        groups[t].sort(key=lambda x: (0 if x.status == "enabled" else 1, x.name))

    result: list[Entry] = []
    for t in ("command", "plugin", "tool"):
        result.extend(groups.get(t, []))
    return result


def _resolve_target(ordered: list[Entry], target: str) -> Entry | None:
    """'3' → 第 3 个条目（1-based）；'web_search' → 按名称查找"""
    if target.isdigit():
        idx = int(target) - 1
        if 0 <= idx < len(ordered):
            return ordered[idx]
        return None  # 编号越界
    # 名称精确匹配
    for item in ordered:
        if item.name == target:
            return item
    return None


# ═══════════════════════════════════════════════════════════════
# 格式化输出（Markdown）
# ═══════════════════════════════════════════════════════════════


def _fmt_list(items: list[Entry]) -> str:
    if not items:
        return "📭 无匹配的拓展"

    # items 已是有序的（来自 _get_ordered_items）
    groups: dict[str, list[Entry]] = {"command": [], "plugin": [], "tool": []}
    for item in items:
        groups.setdefault(item.type, []).append(item)

    labels = {
        "command": "命令 (command)",
        "plugin": "生命周期插件 (plugin)",
        "tool": "工具 (tool)",
    }
    total = len(items)
    disabled_count = sum(1 for i in items if i.status == "disabled")

    lines = [f"### 📦 拓展总览（共 {total} 个" + (f"，{disabled_count} 个已禁用" if disabled_count else "") + "）\n"]

    idx = 0  # 全局连续编号
    for t in ("command", "plugin", "tool"):
        group = groups.get(t, [])
        if not group:
            continue
        lines.append(f"**{labels.get(t, t)}**（{len(group)} 个）\n")
        for item in group:
            idx += 1
            if item.status == "disabled":
                lines.append(f"- ⛔ `{idx}. {item.name}`")
            else:
                marker = " ⚡" if item.user_override else ""
                lines.append(f"- ✅ `{idx}. {item.name}`{marker}")
        lines.append("")

    lines.append("---\n")
    lines.append("`✅ 已启用　⛔ 已禁用　⚡ 用户覆盖　复制编号操作: /option info 3`")

    return "\n".join(lines)


def _fmt_info(item: Entry) -> str:
    lines = [f"### 📋 {item.name}\n"]

    type_label = {"command": "命令 (command)", "plugin": "生命周期插件 (plugin)", "tool": "工具 (tool)"}
    lines.append(f"- **类型**: {type_label.get(item.type, item.type)}")
    if item.description:
        lines.append(f"- **描述**: {item.description}")
    lines.append(f"- **状态**: {'✅ 已启用' if item.status == 'enabled' else '⛔ 已禁用'}")

    if item.user_override:
        # 找到内置路径
        builtin_path = ""
        if item.type == "tool" and item.source_path and not item.source_path.startswith("["):
            bdir = os.path.join(FP_CORE_DIR, "tools", "extensions")
            fname = os.path.basename(item.source_path)
            bp = os.path.join(bdir, fname)
            if os.path.isfile(bp) and bp != item.source_path:
                builtin_path = bp
        if builtin_path:
            lines.append("- **来源**: 用户覆盖 ⚡")
            lines.append(f"  - 主: `{item.source_path}`")
            lines.append(f"  - 内置: `{builtin_path}`")
        else:
            lines.append(f"- **来源**: 用户覆盖 ⚡ (`{item.source_path}`)")
    else:
        src_label = _SOURCE_LABELS.get(item.source, item.source)
        lines.append(f"- **来源**: {src_label} (`{item.source_path}`)")

    c = item.contract

    if item.type == "command":
        aliases = c.get("aliases", [])
        if aliases:
            lines.append(f"- **别名**: `{'`, `'.join(aliases)}`")
        lines.append("\n**接口**\n")
        lines.append("```\nexecute(state, arg: str) -> tuple[bool, str]\n```")

    elif item.type == "plugin":
        lines.append(f"- **版本**: {c.get('version', '?')}")
        hooks = c.get("hooks", [])
        if hooks:
            lines.append(f"\n**注册的钩子**（{len(hooks)} 个）\n")
            for h in hooks:
                hicon = "👀" if h.get("type") == "observe" else "🔄"
                lines.append(f"- {hicon} `{h['hook']}` (pri={h['priority']})")
        else:
            lines.append("\n*未加载，无法读取钩子信息*\n")

    elif item.type == "tool":
        is_core = c.get("core", False)
        if is_core:
            lines.append("- **属性**: ◆ 核心工具（不可禁用）")
        schema = c.get("schema", {})
        if schema and schema.get("parameters"):
            lines.append("\n**参数**\n")
            params = schema["parameters"].get("properties", {})
            required = set(schema["parameters"].get("required", []))
            for pname, pinfo in params.items():
                req = " ⚠️ 必填" if pname in required else ""
                ptype = pinfo.get("type", "?")
                desc = pinfo.get("description", "")
                lines.append(f"- **{pname}** (`{ptype}`{req})")
                if desc:
                    lines.append(f"  - {desc}")

    return "\n".join(lines)


def _fmt_diff(diff_info: dict[str, Any]) -> str:
    if "note" in diff_info:
        return f"ℹ️ {diff_info['note']}"

    lines = [f"### 差异对比：{diff_info['name']}\n"]
    lines.append(f"- **内置**: `{diff_info['builtin_path']}`（{diff_info['builtin_lines']} 行）")
    lines.append(f"- **用户**: `{diff_info['user_path']}`（{diff_info['user_lines']} 行）\n")

    delta = diff_info["user_lines"] - diff_info["builtin_lines"]
    if delta != 0:
        pct = (delta / max(diff_info["builtin_lines"], 1)) * 100
        sign = "+" if delta > 0 else ""
        lines.append(f"- **行数**: 用户版 {sign}{delta} 行（{sign}{pct:.1f}%）")
    else:
        lines.append("- **行数**: 完全一致")

    schema_match = diff_info.get("schema_match")
    if schema_match is not None:
        lines.append(f"- **Schema**: {'完全一致 ✅' if schema_match else '存在差异 ⚠️'}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 命令入口
# ═══════════════════════════════════════════════════════════════


def execute(state: Any, arg: str) -> tuple[bool, str]:
    mgr = OptionManager(state)
    arg = arg.strip()

    # 无参数 → 显示帮助
    if not arg:
        return (True, _fmt_help())

    parts = arg.split()
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd == "list":
        return _handle_list(mgr, args)

    if cmd in ("info", "enable", "disable", "diff"):
        if not args:
            return (True, f"用法: `/option {cmd} <名称或编号>`")
        return _handle_action(mgr, cmd, args[0])

    cmd_list = "`list`, `info`, `enable`, `disable`, `diff`"
    return (
        True,
        f"⚠️ 未知子命令: `{cmd}`\n\n可用子命令: {cmd_list}\n无参数显示帮助: `/option`",
    )


# ═══════════════════════════════════════════════════════════════
# 帮助信息
# ═══════════════════════════════════════════════════════════════


def _fmt_help() -> str:
    return """### 🔧 /option — 统一拓展管理

管理三种拓展机制：命令 (command) / 生命周期插件 (plugin) / 工具 (tool)

**用法**

| 命令 | 说明 |
|------|------|
| `/option` | 显示本帮助 |
| `/option list` | 列出所有拓展（带编号） |
| `/option list command` | 只列命令 |
| `/option list plugin` | 只列插件 |
| `/option list tool` | 只列工具 |
| `/option list enabled` | 只列已启用 |
| `/option list disabled` | 只列已禁用 |
| `/option info <名称/编号>` | 查看详情 |
| `/option enable <名称/编号>` | 启用（`.disabled` → `.py`） |
| `/option disable <名称/编号>` | 禁用（`.py` → `.disabled`） |
| `/option diff <名称/编号>` | 内置 vs 用户版差异对比 |

**编号操作**

`/option list` 中每个条目左侧有编号，可用编号代替名称：

```
/option info 3      → 查看第 3 个拓展
/option disable 5   → 禁用第 5 个拓展
```

**标记说明**

`✅` 已启用　`⛔` 已禁用　`⚡` 用户覆盖
"""


# ═══════════════════════════════════════════════════════════════
# 调度
# ═══════════════════════════════════════════════════════════════


def _handle_list(mgr: OptionManager, args: list[str]) -> tuple[bool, str]:
    type_filter = args[0] if args else "all"
    type_filter = type_filter.lower()
    type_map = {
        "all": "all",
        "cmd": "command",
        "commands": "command",
        "command": "command",
        "plugin": "plugin",
        "plugins": "plugin",
        "tool": "tool",
        "tools": "tool",
        "enabled": "all",
        "on": "all",
        "disabled": "all",
        "off": "all",
    }
    status_filter = "all"
    if type_filter in ("enabled", "on"):
        status_filter = "enabled"
        type_filter = "all"
    elif type_filter in ("disabled", "off"):
        status_filter = "disabled"
        type_filter = "all"

    resolved = type_map.get(type_filter, type_filter)
    if resolved not in ("all", "command", "plugin", "tool"):
        return (True, f"⚠️ 未知类型: {type_filter}（可用: all, command, plugin, tool, enabled, disabled）")

    items = _get_ordered_items(mgr, resolved, status_filter)
    return (True, _fmt_list(items))


def _handle_action(mgr: OptionManager, action: str, target: str) -> tuple[bool, str]:
    """处理 info / enable / disable / diff，支持编号和名称"""
    ordered = _get_ordered_items(mgr)
    entry = _resolve_target(ordered, target)

    if entry is None:
        # 尝试直接传递给 mgr（可能对应禁用的命令等）
        if action in ("enable", "disable"):
            if action == "enable":
                _ok, msg = mgr.enable(target)
            else:
                _ok, msg = mgr.disable(target)
            return (True, msg)
        return (True, f"⚠️ 未找到拓展 '{target}'（使用 `/option list` 查看可用名称/编号）")

    if action == "info":
        return (True, _fmt_info(entry))

    name = entry.name

    if action == "enable":
        _ok, msg = mgr.enable(name)
        return (True, msg)

    if action == "disable":
        _ok, msg = mgr.disable(name)
        return (True, msg)

    if action == "diff":
        result = mgr.diff(name)
        if result is None:
            return (True, f"⚠️ 未找到拓展 '{name}'")
        return (True, _fmt_diff(result))

    return (True, f"⚠️ 未知操作: {action}")
