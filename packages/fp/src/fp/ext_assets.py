"""ext 资产模型 — 三目录路径解析、资产类型、老结构识别

三来源目录（优先级低→高：fetched → public → private）：
  {DATA}/fetched/  外来资产（只读，来自 fp ext fetch）
  {DATA}/public/   本地公开资产（git 管理，可 push 分享）
  {DATA}/private/  本地私有资产（git 管理，禁 remote）

每种来源内部按资产类型分目录：
  tools/extensions/  工具插件（*_plugin.py）
  commands/          命令模块（*.py）
  plugins/           Plugin 插件（*_plugin.py 或包）
  memory/            记忆（*.md）

设计原则：本模块是纯路径层，不依赖 fp_core 的加载逻辑；
仅复用 fp_core.platform_utils.get_data_dir（跨平台路径单一真相源）。
"""

import os

from fp_core.platform_utils import get_data_dir

# 资产类型 → 相对子路径（从来源根算起）
ASSET_LAYOUTS: dict[str, tuple[str, ...]] = {
    "tools": ("tools", "extensions"),
    "commands": ("commands",),
    "plugins": ("plugins",),
    "memory": ("memory",),
}
ASSET_TYPES: tuple[str, ...] = tuple(ASSET_LAYOUTS)

# 来源（优先级从低到高）
SOURCES: tuple[str, ...] = ("fetched", "public", "private")

# 老结构路径（迁移前，{DATA} 下直挂）
LEGACY_LAYOUTS: dict[str, tuple[str, ...]] = {
    "tools": ("tools", "extensions"),
    "commands": ("commands",),
    "plugins": ("plugins",),
    "memory": ("memory",),
}
# tools 的另一个老目录：tools/plugins（工具型插件旧位置，迁移时并入 extensions）
LEGACY_TOOLS_PLUGINS: tuple[str, ...] = ("tools", "plugins")


def data_dir() -> str:
    """fp 数据目录（跨平台，复用 fp_core 纯路径层）。"""
    return get_data_dir()


# ── 三来源路径 ─────────────────────────────────────────────────


def source_root(source: str) -> str:
    """返回某来源的根目录，如 {DATA}/private。"""
    return os.path.join(data_dir(), source)


def source_dir(source: str, asset_type: str) -> str:
    """返回某来源某类型的目录，如 {DATA}/private/tools/extensions。"""
    return os.path.join(source_root(source), *ASSET_LAYOUTS[asset_type])


def all_source_dirs(asset_type: str) -> list[str]:
    """返回某类型的三来源目录列表（优先级从低到高：fetched → public → private）。

    加载器按此顺序扫描，后加载覆盖先加载（private 胜出）。
    """
    return [source_dir(s, asset_type) for s in SOURCES]


# ── 老结构识别 ─────────────────────────────────────────────────


def legacy_dir(asset_type: str) -> str:
    """返回老结构目录（迁移前）。"""
    return os.path.join(data_dir(), *LEGACY_LAYOUTS[asset_type])


def detect_legacy() -> list[tuple[str, str]]:
    """检测现存老结构目录，返回 [(资产类型, 老目录), ...]。

    tools 可能有两处老目录（tools/extensions 与 tools/plugins），都列出。
    仅返回非空目录（空目录直接清理即可，无需迁移）。
    """
    found: list[tuple[str, str]] = []
    for atype in ASSET_LAYOUTS:
        d = legacy_dir(atype)
        if os.path.isdir(d) and os.listdir(d):
            found.append((atype, d))
    tp = os.path.join(data_dir(), *LEGACY_TOOLS_PLUGINS)
    if os.path.isdir(tp) and os.listdir(tp):
        found.append(("tools", tp))
    return found


# ── 元数据文件 ─────────────────────────────────────────────────


def registry_path() -> str:
    """fetched 资产注册表 JSON 路径。"""
    return os.path.join(data_dir(), "ext-registry.json")


def audit_path() -> str:
    """两段式审计日志 jsonl 路径。"""
    return os.path.join(data_dir(), "ext-audit.jsonl")


def trash_dir() -> str:
    """remove 的回收站目录（可恢复）。"""
    return os.path.join(data_dir(), ".trash")
