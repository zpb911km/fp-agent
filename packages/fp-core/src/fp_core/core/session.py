"""
会话管理

管理对话历史和会话文件。每个会话文件格式：

  第 1 行:  {"__meta__": true, "id": "s_xxx", "created": "...", "updated": "...", ...}
  第 2+ 行: {"role": "user", "content": "..."}   （按时间正序，最新在末尾）

无独立 meta 文件 / _current 文件。启动时扫描目录，
取 updated 最新的会话作为"上一个会话"。
"""

import contextlib
import json
import os
import re
from datetime import datetime
from typing import Any

from fp_core import config

SESSIONS_DIR = config.SESSIONS_DIR


# ── 辅助：会话文件名模式 ──────────────────────────

SID_PATTERN = re.compile(r"^s_\d{6}_\d{12,}.*\.jsonl$")  # 微秒级 sid


def _is_session_file(filename: str) -> bool:
    return bool(SID_PATTERN.match(filename))


def _extract_sid(filename: str) -> str:
    """从文件名提取原始 sid（去掉 summary 后缀）。"""
    # s_260606_1600_summary_123456.jsonl → s_260606_1600
    match = re.match(r"^(s_\d{6}_\d{12,})", filename)
    return match.group(1) if match else filename.replace(".jsonl", "")


# ── 会话文件（嵌入 meta） ─────────────────────────


def _default_meta(sid: str) -> dict:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "__meta__": True,
        "id": sid,
        "created": now,
        "updated": now,
        "summary": "",
        "message_count": 0,
    }


def _generate_sid() -> str:
    """生成唯一会话 ID（微秒级 + 防冲突后缀）。"""
    base = datetime.now().strftime("s_%y%m%d_%H%M%S%f")  # 含微秒
    sid = base
    # 如果文件已存在，加自增后缀
    for i in range(100):
        if not os.path.exists(_session_path(sid)):
            return sid
        sid = f"{base}_{i}"
    # 极端情况：加随机数
    import random

    return f"{base}_{random.randint(1000, 9999)}"


def _session_path(sid: str) -> str:
    """返回 sid 对应的 .jsonl 文件路径（不含 summary 后缀的原始文件）。"""
    return os.path.join(SESSIONS_DIR, f"{sid}.jsonl")


def _read_meta_from_file(path: str) -> dict | None:
    """读取会话文件第一行中的 meta 信息。"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            first = f.readline().strip()
            if first:
                meta = json.loads(first)
                if meta.get("__meta__"):
                    return meta
    except Exception:
        pass
    return None


def _write_meta_to_file(path: str, meta: dict) -> bool:
    """重写会话文件的第一行（meta header）。"""
    if not os.path.exists(path):
        return False
    try:
        content = json.dumps(meta, ensure_ascii=False)
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        with open(path, "w", encoding="utf-8") as f:
            f.write(content + "\n")
            f.writelines(lines[1:])
        return True
    except Exception:
        return False


def _find_latest_session() -> str | None:
    """扫描 sessions 目录，返回 updated 最新的会话 sid。
    若无任何会话，返回 None。"""
    latest_sid = None
    latest_time = ""

    try:
        for fname in os.listdir(SESSIONS_DIR):
            if not _is_session_file(fname):
                continue
            path = os.path.join(SESSIONS_DIR, fname)
            meta = _read_meta_from_file(path)
            if meta and meta.get("updated", "") > latest_time:
                latest_time = meta["updated"]
                latest_sid = meta.get("id", _extract_sid(fname))
    except Exception:
        pass

    return latest_sid


# ── SessionManager ────────────────────────────────


class SessionManager:
    """会话管理器 — 只负责持久化，不再持有 _context"""

    _session_id: str
    _meta: dict

    def __init__(self, resume: str | None = None):
        """
        resume=None/False → 创建新会话（默认）
        resume=True       → 续最近会话
        resume="auto"     → 续最近会话
        resume="s_xxx"    → 续指定会话
        """
        os.makedirs(SESSIONS_DIR, exist_ok=True)

        # 类型归一化：bool → str/None，统一进入后续分支
        if resume is None or resume is False:
            resume = None
        elif resume is True:
            resume = "auto"

        if resume is not None:
            latest = resume if resume.startswith("s") else _find_latest_session()
            if latest and self._session_exists(latest):
                self._session_id = latest
                self._meta = self._load_meta_from_session()
                return

        # 默认：创建新会话
        self._session_id = self._init_session()
        self._meta = self._load_meta_from_session()

    # ── 内部工具 ──────────────────────────────────

    @staticmethod
    def _session_exists(sid: str) -> bool:
        path = _session_path(sid)
        return os.path.exists(path)

    def _session_path(self, sid: str | None = None) -> str:
        sid = sid or self._session_id
        return _session_path(sid)

    def _load_meta_from_session(self, sid: str | None = None) -> dict:
        """从会话文件读取 meta。"""
        sid = sid or self._session_id
        path = self._session_path(sid)
        meta = _read_meta_from_file(path)
        if meta is None:
            meta = _default_meta(sid)
        return meta

    def _write_meta(self, meta: dict | None = None):
        """将 meta 写回文件第一行。"""
        if meta is None:
            meta = self._meta
        path = self._session_path()
        _write_meta_to_file(path, meta)

    # ── 会话生命周期 ──────────────────────────────

    def _init_session(self) -> str:
        """创建新会话。"""
        sid = _generate_sid()
        meta = _default_meta(sid)
        path = _session_path(sid)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        self._meta = meta
        return sid

    @property
    def session_id(self) -> str:
        return self._session_id

    def get_session_path(self, sid: str | None = None) -> str:
        """获取指定会话的文件路径（公共 API）"""
        return self._session_path(sid)

    def list_sessions(self) -> dict[str, dict[str, Any]]:
        """列出所有会话及其 meta。扫描 sessions 目录。"""
        sessions = {}
        try:
            for fname in os.listdir(SESSIONS_DIR):
                if not _is_session_file(fname):
                    continue
                path = os.path.join(SESSIONS_DIR, fname)
                meta = _read_meta_from_file(path)
                if meta:
                    sid = meta.get("id", _extract_sid(fname))
                    sessions[sid] = meta
        except Exception:
            pass
        return sessions

    def switch_session(self, sid: str) -> bool:
        """切换到指定会话。"""
        if not self._session_exists(sid):
            return False
        self._session_id = sid
        self._meta = self._load_meta_from_session()
        return True

    def create_session(self) -> str:
        """创建新会话并切换过去。"""
        self._session_id = self._init_session()
        self._meta = self._load_meta_from_session()
        return self._session_id

    def delete_session(self, sid: str) -> bool:
        """删除指定会话文件。不能删除当前会话。返回是否成功。"""
        if sid == self._session_id:
            return False  # 不允许删除当前会话
        path = _session_path(sid)
        if not os.path.exists(path):
            return False
        try:
            os.remove(path)
            return True
        except Exception:
            return False

    def clear_session_file(self):
        """清空当前会话文件（重置为默认 meta，删除历史消息）。"""
        self._meta = _default_meta(self._session_id)
        path = self._session_path()
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(self._meta, ensure_ascii=False) + "\n")

    def resume_latest(self) -> bool:
        """尝试续最近会话。成功返回 True，否则创建新会话。"""
        latest = _find_latest_session()
        if latest and self._session_exists(latest):
            self._session_id = latest
            self._meta = self._load_meta_from_session()
            return True
        # 没有历史会话 → 创建新会话
        self._session_id = self._init_session()
        self._meta = self._load_meta_from_session()
        return False

    # ── 消息存储（正序，最新在文件末尾） ──────────

    def save_message(self, role: str, content: str, **kwargs):
        """追加一条消息到文件末尾，并更新文件内嵌的 meta。"""
        msg = {"role": role, "content": content}
        for k, v in kwargs.items():
            if v:
                msg[k] = v

        path = self._session_path()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        self._meta["message_count"] = self._meta.get("message_count", 0) + 1
        self._meta["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._write_meta(self._meta)

    def save_context(self, context: list[dict[str, Any]]):
        """将完整上下文写入文件（重写）。正序写入，最新在末尾。"""
        path = self._session_path()

        lines = []
        msg_count = 0
        for i, msg in enumerate(context):
            if msg.get("role") == "system" and i == 0:
                continue  # 只跳过第一条 system prompt（后续由 load_context 重新加载）
            msg_count += 1
            save_msg = {"role": msg["role"], "content": msg.get("content", "")}
            for k in ("tool_calls", "tool_call_id", "reasoning_content"):
                if msg.get(k):
                    save_msg[k] = msg[k]
            lines.append(json.dumps(save_msg, ensure_ascii=False))

        self._meta["message_count"] = msg_count
        self._meta["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(self._meta, ensure_ascii=False) + "\n")
                for line in lines:
                    f.write(line + "\n")
            os.replace(tmp, path)
        except Exception:
            with contextlib.suppress(Exception):
                os.remove(tmp)

    def load_context(self, system_prompt: str) -> list[dict[str, Any]]:
        """加载上下文（system + 历史消息，正序）。"""
        context = [{"role": "system", "content": system_prompt}]
        path = self._session_path()

        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    lines = f.readlines()
                for line in lines[1:]:
                    line = line.strip()
                    if line:
                        msg = json.loads(line)
                        context.append(msg)
            except Exception:
                pass

        return context

    def update_meta(self, sid: str | None = None, **kwargs):
        """更新指定会话的内嵌 meta 字段。"""
        sid = sid or self._session_id
        path = _session_path(sid)
        meta = _read_meta_from_file(path)
        if meta is None:
            return
        meta.update(kwargs)
        meta["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _write_meta_to_file(path, meta)
        if sid == self._session_id:
            self._meta = meta
