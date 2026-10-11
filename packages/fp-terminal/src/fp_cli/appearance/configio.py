"""config.json 的读写 — 外观设置的唯一落盘通道。

写入只有一个入口 :func:`persist_tui`，它接受**点路径 → 值**的批量更新，
并按需创建中间层。面板（panel.py）与主题 watcher（tui.py）都走它：

    persist_tui({"tui.theme": "dracula"})
    persist_tui({"tui.styles.info": "bold green"})
    persist_tui({"tui.symbols.bot": UNSET})   # UNSET = 删除该键 → 回落默认

三条安全线（历史上踩过：一次写盘把用户 LLM 配置冲成只剩 tui 段的残骸）：
1. **只在现有且合法**的 config.json 上改写——读不到 / 解析失败 / 根不是 dict
   → 一个字节都不写，返回 False
2. **值没变就不碰文件**（含「删除一个本来就不存在的键」）——避免每次启动都重写
3. 先写 ``.tmp`` 再 ``os.replace`` 原子替换；失败静默（外观偏好写不进去
   不该打断交互）并把临时文件清掉

读取侧一律「坏数据 = 视同无配置」，永不抛异常。
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Mapping
from typing import Any, cast

from fp_core.platform_utils import get_config_dir


class _Unset:
    """``UNSET`` 哨兵类型：表示「删除该键」（回落默认），区别于 None。"""

    _instance: _Unset | None = None

    def __new__(cls) -> _Unset:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"


#: persist_tui 的值位传它 = 删除该点路径（config 里没有该键 = 用出厂默认）
UNSET = _Unset()


def load_config(path: str | None = None) -> dict[str, Any]:
    """读整个 config.json。任何异常 → ``{}``（视同无配置）。"""
    cfg_path = path or os.path.join(get_config_dir(), "config.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            data: Any = json.load(f)
    except (OSError, ValueError, AttributeError, TypeError):
        return {}
    return cast(dict[str, Any], data) if isinstance(data, dict) else {}


def load_tui_config(path: str | None = None) -> dict[str, Any]:
    """读取 config.json 的 ``tui`` 段。任何异常 → ``{}``（视同无配置）。"""
    tui: object = load_config(path).get("tui")
    return cast(dict[str, Any], tui) if isinstance(tui, dict) else {}


def persist_tui(updates: Mapping[str, Any], path: str | None = None) -> bool:
    """把 ``{点路径: 值}`` 写回 config.json（值 ``UNSET`` = 删除该键）。

    点路径一律以 ``tui`` 开头（如 ``tui.styles.info``）；中间层缺失时自动补建
    （删 UNSET 除外——不创建）。
    返回 True 表示文件确实被改（无实际变化 → False，且不碰文件）。
    """
    cfg_path = path or os.path.join(get_config_dir(), "config.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            data: Any = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False

    root = cast(dict[str, Any], data)
    before = _fingerprint(root)
    for dotted, value in updates.items():
        parts = [p for p in dotted.split(".") if p]
        if not parts:
            continue
        if value is UNSET:
            _delete(root, parts)
        else:
            _assign(root, parts, value)

    if _fingerprint(root) == before:
        return False  # 无实际变化 → 不碰文件（也避免每次启动都重写）

    tmp = f"{cfg_path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(root, f, indent=2, ensure_ascii=False)
        os.replace(tmp, cfg_path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return False
    return True


def _fingerprint(root: dict[str, Any]) -> str:
    """结构指纹：比对「写前 / 写后」是否真的变了。"""
    return json.dumps(root, sort_keys=True, ensure_ascii=False)


def _assign(root: dict[str, Any], parts: list[str], value: object) -> None:
    """沿点路径写入，中间层缺失或类型不对时补建 / 覆盖成 dict。"""
    node: dict[str, Any] = root
    for part in parts[:-1]:
        child: object = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = cast(dict[str, Any], child)
    node[parts[-1]] = value


def _delete(root: dict[str, Any], parts: list[str]) -> bool:
    """删除点路径叶子；顺带剪掉变空的中间层（不创建任何层）。"""
    if len(parts) == 1:
        if parts[0] in root:
            del root[parts[0]]
            return True
        return False
    child = root.get(parts[0])
    if not isinstance(child, dict):
        return False
    removed = _delete(cast(dict[str, Any], child), parts[1:])
    if removed and not child:
        del root[parts[0]]  # tui.styles 空了就删 styles，空了就删 tui——等价于没配过
    return removed


__all__ = ["UNSET", "load_config", "load_tui_config", "persist_tui"]
