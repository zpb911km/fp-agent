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
from typing import Any, cast

from fp_core import config

SESSIONS_DIR = config.SESSIONS_DIR

# 会话列表的展示标签（list_sessions 回填，前端不再退化成显示 sid）
EMPTY_SESSION_LABEL = "(空白会话)"
NO_SUMMARY_LABEL = "(无摘要)"


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


def _default_meta(sid: str) -> dict[str, Any]:
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


def session_file_path(sid: str) -> str:
    """公开：sid → 会话 .jsonl 文件路径（api.ctl.sessions.path 的底层）"""
    return _session_path(sid)


def session_trash_dir() -> str:
    """公开：空会话清理的回收目录（`{SESSIONS_DIR}/.trash/`）。

    清理是**移动**而非删除——`.trash` 不匹配会话文件名模式，不会被
    `list_sessions()` 扫描到；需要时手工搬回即可。
    """
    return os.path.join(SESSIONS_DIR, ".trash")


def _read_meta_from_file(path: str) -> dict[str, Any] | None:
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


def _write_meta_to_file(path: str, meta: dict[str, Any]) -> bool:
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


def summarize_text(content: str, limit: int = 20) -> str:
    """摘要策略：文本首 N 字符（换行转空格）——全链路唯一的摘要生成规则。

    save_and_summarize、会话列表回填、subagent 兜底共用，避免各自漂移。
    """
    return content.strip().replace("\n", " ")[:limit]


def _scan_messages(path: str) -> tuple[bool, str, int]:
    """扫描会话文件正文。

    Returns:
        `(是否有消息行, 最后一条 user 消息原文, 消息行数)`；
        文件不存在 → `(False, "", 0)`；**读取失败 → `(True, "", 0)`**
        （保守当有内容：宁可让列表多显示一个，也不误判有效历史）。
    """
    if not os.path.exists(path):
        return False, "", 0
    has_msgs = False
    last_user = ""
    count = 0
    try:
        with open(path, encoding="utf-8") as f:
            f.readline()  # 跳过 meta 行
            for line in f:
                line = line.strip()
                if not line:
                    continue
                has_msgs = True
                count += 1
                try:
                    msg: Any = json.loads(line)
                except Exception:
                    continue
                if not isinstance(msg, dict):
                    continue
                d = cast("dict[str, Any]", msg)
                if d.get("role") == "user":
                    content = d.get("content")
                    if isinstance(content, str) and content.strip():
                        last_user = content
    except Exception:
        return True, "", 0
    return has_msgs, last_user, count


def _find_latest_session() -> str | None:
    """扫描 sessions 目录，返回 updated 最新的**非空**会话 sid。

    优先级：有消息的会话 > meta-only 占位文件。占位文件（0 消息）是
    「新建即弃」会话的残留（/new、前端新建、空进程退出），把它当"最近会话"
    会让 `fp -r` / `/resume latest` 落到一个空会话上——所以只有在目录里
    **一个非空会话都没有**时才回退到占位文件。若无任何会话，返回 None。
    """
    latest_sid: str | None = None
    latest_time: str = ""
    latest_used_sid: str | None = None
    latest_used_time: str = ""

    try:
        for fname in os.listdir(SESSIONS_DIR):
            if not _is_session_file(fname):
                continue
            path = os.path.join(SESSIONS_DIR, fname)
            meta = _read_meta_from_file(path)
            if not meta:
                continue
            updated = meta.get("updated", "")
            sid = meta.get("id") or _extract_sid(fname)
            if updated > latest_time:
                latest_time = updated
                latest_sid = sid
            if meta.get("message_count", 0) > 0 and updated > latest_used_time:
                latest_used_time = updated
                latest_used_sid = sid
    except Exception:
        pass

    return latest_used_sid or latest_sid


def update_session_meta(sid: str, **kwargs: Any) -> bool:
    """模块级函数：更新指定会话的 meta 字段（不依赖 SessionManager 实例）。

    与 SessionManager.update_meta 不同，本函数在会话文件不存在时
    会自动创建文件（用于 subagent 兜底：子进程被 SIGKILL 后补写 meta）。
    """
    path = _session_path(sid)
    meta = _read_meta_from_file(path)
    if meta is None:
        meta = _default_meta(sid)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        except Exception:
            return False
    meta.update(kwargs)
    meta["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return _write_meta_to_file(path, meta)


# ── 模块级"当前会话"注册 ─────────────────────────
# 供同进程内的其他模块（如 subagent 插件）读取当前活动会话 ID。
# 多 Agent 实例时以最后创建的为准（通常即当前工作 Agent）。

_current_session_id: str | None = None


def _register_current_session(sid: str) -> None:
    """内部：注册当前活动会话 ID（SessionManager 初始化时调用）。"""
    global _current_session_id
    _current_session_id = sid


def get_current_session_id() -> str | None:
    """获取当前进程内最近创建的会话 ID。"""
    return _current_session_id


# ── SessionManager ────────────────────────────────


class SessionManager:
    """会话管理器 — 只负责持久化，不再持有 _context"""

    _session_id: str
    _meta: dict[str, Any]

    def __init__(self, resume: str | bool | None = None, new_sid: str | None = None):
        """
        resume=None/False → 创建新会话（默认）
        resume=True       → 续最近会话
        resume="auto"     → 续最近会话
        resume="s_xxx"    → 续指定会话
        new_sid           → 使用预置 sid 创建新会话（优先级最高，
                            供 subagent 等需要"父进程预知子会话 id"的场景）
        """
        os.makedirs(SESSIONS_DIR, exist_ok=True)

        # 预置 sid：直接使用，不经过 resume 逻辑
        if new_sid:
            self._session_id = new_sid
            self._meta = self._load_meta_from_session()
            _register_current_session(self._session_id)
            return

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
                _register_current_session(self._session_id)
                return

        # 默认：分配新会话 ID（惰性文件创建，首次写入时自动生成文件）
        self._session_id = self._allocate_session()
        self._meta = self._load_meta_from_session()
        _register_current_session(self._session_id)

    # ── 内部工具 ──────────────────────────────────

    @staticmethod
    def _session_exists(sid: str) -> bool:
        path = _session_path(sid)
        return os.path.exists(path)

    def _session_path(self, sid: str | None = None) -> str:
        sid = sid or self._session_id
        return _session_path(sid)

    def _load_meta_from_session(self, sid: str | None = None) -> dict[str, Any]:
        """从会话文件读取 meta。"""
        sid = sid or self._session_id
        path = self._session_path(sid)
        meta = _read_meta_from_file(path)
        if meta is None:
            meta = _default_meta(sid)
        return meta

    def _write_meta(self, meta: dict[str, Any] | None = None) -> None:
        """将 meta 写回文件第一行。"""
        if meta is None:
            meta = self._meta
        path = self._session_path()
        _write_meta_to_file(path, meta)

    # ── 会话生命周期 ──────────────────────────────

    def _allocate_session(self, sid: str | None = None) -> str:
        """分配新会话 ID（惰性文件创建）。

        只在内存中分配 ID 和 meta，不写入磁盘。
        首次通过 save_message() / save_context() / clear_session_file()
        写入数据时，文件会被自动创建。
        避免每次 Agent 实例化都产生空会话文件。

        Args:
            sid: 可指定 sid（如 subagent 预生成），None 时自动生成
        """
        if sid:
            self._meta = _default_meta(sid)
            return sid
        sid = _generate_sid()
        self._meta = _default_meta(sid)
        return sid

    @property
    def session_id(self) -> str:
        return self._session_id

    def get_session_path(self, sid: str | None = None) -> str:
        """获取指定会话的文件路径（公共 API）"""
        return self._session_path(sid)

    def list_sessions(self) -> dict[str, dict[str, Any]]:
        """列出所有会话及其 meta —— **展示就绪**（所有前端共用同一口径）。

        `/resume list`、WebUI 会话面板、ACP session/list 都走这里，故三件事
        统一在此完成，而不是让每个前端各写一遍兜底：

          1. **合并当前会话的内存 meta**：会话文件是惰性创建的，刚 `/new`
             出来的会话在盘上还没文件，但它必须出现在列表里。此前这条可见性
             是靠「写 meta-only 占位文件」换来的，代价就是列表被 0 长度记录
             刷屏——现在改为列表侧合并，不再为可见性写文件。
          2. **隐藏 0 长度会话**：文件里没有消息行、且不是当前会话 → 不返回
             （列表是用来找「能续接的对话」的，无一行消息的记录无从续接）。
             判定以**文件实际内容**为准（`_scan_messages`），不信
             `message_count`，避免误伤 meta 滞后的历史会话。
          3. **摘要回填**：`meta.summary` 缺失时从文件最后一条 user 消息现场
             推导并写回（老会话没走 save_and_summarize 的一次性补齐，写回
             不动 `updated` 时间戳以免打乱排序）；仍算不出时给展示标签
             「(空白会话)」/「(无摘要)」，不再让前端退化成显示 sid。

        Returns:
            `{sid: meta}`；meta 的 `summary` 字段保证非空可直接展示。
        """
        sessions: dict[str, dict[str, Any]] = {}
        try:
            for fname in os.listdir(SESSIONS_DIR):
                if not _is_session_file(fname):
                    continue
                path = os.path.join(SESSIONS_DIR, fname)
                meta = _read_meta_from_file(path)
                if not meta:
                    continue
                sid = str(meta.get("id") or _extract_sid(fname))
                shown = self._display_ready_meta(sid, meta, path)
                if shown is not None:
                    sessions[sid] = shown
        except Exception:
            pass

        # 当前会话可能还没有文件（惰性创建）→ 用内存 meta 补进列表
        if self._session_id not in sessions:
            mem = dict(self._meta)
            mem.setdefault("message_count", 0)
            if not (mem.get("summary") or "").strip():
                mem["summary"] = EMPTY_SESSION_LABEL
            sessions[self._session_id] = mem

        return sessions

    def _display_ready_meta(self, sid: str, meta: dict[str, Any], path: str) -> dict[str, Any] | None:
        """把原始 meta 加工成展示就绪；返回 None = 该会话在列表中不可见。"""
        summary_raw = (meta.get("summary") or "").strip()
        count = meta.get("message_count", 0)

        # 快路径：有消息也有摘要 → 不读文件正文
        need_scan = count == 0 or summary_raw in ("", "empty_session")
        if need_scan:
            has_msgs, last_user, actual_count = _scan_messages(path)
        else:
            has_msgs, last_user, actual_count = True, "", count

        if not has_msgs:
            # 0 长度会话一律不进列表（当前会话除外）——列表是用来找
            # 「能续接的对话」的，没有一行消息的记录无从续接，留着只会
            # 干扰检索；文件本身留在盘上（或被 prune 搬进 .trash/）可查证。
            if sid != self._session_id:
                return None
            out = dict(meta)
            out["summary"] = EMPTY_SESSION_LABEL
            out["message_count"] = 0
            return out

        # 有消息：摘要缺失 → 从文件回填（并写回，一次性自愈）
        out = dict(meta)
        if need_scan:
            out["message_count"] = max(int(count or 0), actual_count)
        summary = summary_raw
        if summary == "empty_session":
            summary = ""
        if not summary and last_user:
            summary = summarize_text(last_user)
            if meta.get("source") == "subagent" and not summary.startswith("[subagent] "):
                summary = f"[subagent] {summary}"
            healed = dict(meta)
            healed["summary"] = summary
            healed["message_count"] = out["message_count"]
            _write_meta_to_file(path, healed)  # 不碰 updated，排序不受影响
        if not summary:
            summary = NO_SUMMARY_LABEL
        out["summary"] = summary
        return out

    def switch_session(self, sid: str) -> bool:
        """切换到指定会话。"""
        if not self._session_exists(sid):
            return False
        self._session_id = sid
        self._meta = self._load_meta_from_session()
        _register_current_session(sid)  # 同步进程级「当前会话」（get_current_session_id 的语义）
        return True

    def create_session(self, sid: str | None = None) -> str:
        """创建新会话并切换过去（惰性文件创建）。

        Args:
            sid: 可指定 sid（如 subagent 预生成），None 时自动生成
        """
        self._session_id = self._allocate_session(sid)
        self._meta = self._load_meta_from_session()
        _register_current_session(self._session_id)  # 同步进程级「当前会话」
        return self._session_id

    def delete_session(self, sid: str, force: bool = False) -> bool:
        """删除指定会话文件。不能删除当前会话（除非 force=True）。返回是否成功。"""
        if not force and sid == self._session_id:
            return False  # 不允许删除当前会话
        path = _session_path(sid)
        if not os.path.exists(path):
            return False
        try:
            os.remove(path)
            return True
        except Exception:
            return False

    def clear_session_file(self) -> None:
        """清空当前会话文件（重置为默认 meta，删除历史消息）。

        惰性约定：当前会话**还没有文件**时只重置内存 meta、不落盘——
        「清一个本就空的会话」不该凭空造出 0 长度会话文件（这正是会话
        列表被占位文件刷屏的来源之一）。当前会话在列表中的可见性由
        `list_sessions()` 合并在内存 meta 保证，不再依赖占位文件。
        """
        self._meta = _default_meta(self._session_id)
        path = self._session_path()
        if not os.path.exists(path):
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(self._meta, ensure_ascii=False) + "\n")

    def resume_latest(self) -> bool:
        """尝试续最近会话。成功返回 True，否则创建新会话。"""
        latest = _find_latest_session()
        if latest and self._session_exists(latest):
            self._session_id = latest
            self._meta = self._load_meta_from_session()
            return True
        # 没有历史会话 → 分配新会话 ID（惰性文件创建）
        self._session_id = self._allocate_session()
        self._meta = self._load_meta_from_session()
        return False

    def prune_empty_sessions(self) -> list[str]:
        """清理「0 长度会话文件」——移动到 `session_trash_dir()`（不真删）。

        保守判定，三条全中才动（宁可漏清也不误伤有效历史）：
          1. 首行是可解析的 meta；
          2. 文件正文**没有任何消息行**（看实际内容，不信 message_count，
             读不出来也视为有内容而跳过）；
          3. 不是当前会话。

        有无摘要不再作为判据——`.trash/` 里连文件带摘要一起保留，
        搬回来即可复原；判据只认「有没有真实消息」。

        Returns:
            被移走的 sid 列表。
        """
        moved: list[str] = []
        trash = session_trash_dir()
        try:
            entries = sorted(os.listdir(SESSIONS_DIR))
        except Exception:
            return moved
        for fname in entries:
            if not _is_session_file(fname):
                continue
            path = os.path.join(SESSIONS_DIR, fname)
            meta = _read_meta_from_file(path)
            if not meta:
                continue  # 读不出 meta → 不碰
            sid = str(meta.get("id") or _extract_sid(fname))
            if sid == self._session_id:
                continue
            has_msgs, _, _ = _scan_messages(path)
            if has_msgs:
                continue
            try:
                os.makedirs(trash, exist_ok=True)
                os.replace(path, os.path.join(trash, fname))
                moved.append(sid)
            except Exception:
                continue
        return moved

    # ── 惰性文件创建 ──────────────────────────────

    def _ensure_file(self) -> None:
        """确保会话文件存在（惰性文件创建的核心）。

        如果文件不存在，用当前 meta 创建文件并写入首行。
        由 save_message() / save_context() 在首次写入前调用。
        """
        path = self._session_path()
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(self._meta, ensure_ascii=False) + "\n")

    # ── 消息存储（正序，最新在文件末尾） ──────────

    def save_message(self, role: str, content: str, **kwargs: Any) -> None:
        """追加一条消息到文件末尾，并更新文件内嵌的 meta。

        首次调用时自动创建会话文件（惰性文件创建）。
        """
        self._ensure_file()
        msg: dict[str, Any] = {"role": role, "content": content}
        for k, v in kwargs.items():
            if v:
                msg[k] = v

        path = self._session_path()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        self._meta["message_count"] = self._meta.get("message_count", 0) + 1
        self._meta["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if role == "user":
            self._refresh_summary([msg])
        self._write_meta(self._meta)

    def _refresh_summary(self, messages: list[dict[str, Any]]) -> None:
        """让 `meta.summary` 跟随最后一条 user 消息（**摘要随写随更**）。

        两条落盘路径都调用它：`save_context`（全量重写，生产主路径）与
        `save_message`（追加）。摘要因此在消息产生的那一刻就已在盘上，
        进程被 SIGKILL / 崩溃也不丢——会话列表不必等 `save_and_summarize`
        （切换/退出）才拿得到摘要。没有 user 消息时保持原摘要不动。
        """
        last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        if last_user is None:
            return
        content = last_user.get("content")
        if not isinstance(content, str) or not content.strip():
            return
        summary = summarize_text(content)
        if self._meta.get("source") == "subagent" and not summary.startswith("[subagent] "):
            summary = f"[subagent] {summary}"
        self._meta["summary"] = summary

    def save_context(self, context: list[dict[str, Any]]) -> None:
        """将完整上下文写入文件。context 应为 to_serializable() 的输出（无 system prompt）。"""
        # 防御性过滤：防止误传入 system 消息
        context = [m for m in context if m.get("role") != "system"]
        path = self._session_path()

        if not context:
            return

        lines = [json.dumps(msg, ensure_ascii=False) for msg in context]

        self._meta["message_count"] = len(context)
        self._meta["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._refresh_summary(context)  # 摘要随写随更（生产主路径是本方法）

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

    def ensure_on_disk(self) -> bool:
        """确保会话文件在磁盘上存在；不存在则写 meta-only 占位文件。

        会话文件是惰性创建的（见 _allocate_session）：空会话（仅 system）
        经 save_context 会因「过滤后为空」被跳过，且 save_context 的写失败
        会被静默吞掉。reload 激活核心（core.handoff.perform_exec_reload）
        依赖文件存在才能让新进程 resume 恢复——此处无条件兜底并把
        「是否真的在盘上」作为可返回的事实交给调用方硬校验。

        Returns:
            True = 会话文件当前存在于磁盘；False = 占位写入失败。
        """
        path = self._session_path()
        if os.path.exists(path):
            return True
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(self._meta, ensure_ascii=False) + "\n")
            os.replace(tmp, path)
            return True
        except Exception:
            with contextlib.suppress(Exception):
                os.remove(tmp)
            return False

    def load_context(self, system_prompt: str) -> list[dict[str, Any]]:
        """加载上下文历史消息（不含 system prompt，与 save_context 对称）。"""
        context: list[dict[str, Any]] = []
        path = self._session_path()

        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    lines = f.readlines()
                for line in lines[1:]:
                    line = line.strip()
                    if line:
                        msg = json.loads(line)
                        if msg.get("role") == "system":
                            continue
                        context.append(msg)
            except Exception:
                pass

        return context

    @property
    def meta(self) -> dict[str, Any]:
        """获取当前会话的 meta 信息（只读视图）"""
        return dict(self._meta)

    def save_and_summarize(self, messages: list[dict[str, Any]], session_id: str | None = None) -> str:
        """保存会话上下文并生成摘要（统一入口）。

        所有会话切换/退出路径都应调用此方法，确保摘要生成逻辑一致。
        摘要策略：取最后一条用户消息的前 20 字符，换行转空格
        （`summarize_text`）。subagent 会话（meta.source == "subagent"）
        自动加 "[subagent] " 前缀。

        不变量：
          - **消息为空且文件不在盘上 → 完全 no-op**（不创建 0 长度会话文件）。
            空转的进程退出、`/new` 前的落盘都不该留下占位文件——这正是
            会话列表被 0 长度记录刷屏的主因。
          - **已有摘要不被空结果抹掉**：算不出新摘要时沿用盘上旧摘要，
            两者皆空才写 `empty_session` 标记（文件已存在时）。

        Args:
            messages: to_serializable() 输出的非 system 消息列表
            session_id: 目标会话 ID（None 表示当前会话）

        Returns:
            摘要文本；无可保存内容且文件不在盘上时返回 ""
        """
        target_sid = session_id or self._session_id
        target_path = _session_path(target_sid)

        # 1. 持久化消息（空 context 时 save_context 是 no-op，不建文件）
        self.save_context(messages)

        # 2. 从最后一条用户消息生成摘要
        summary: str = ""
        last_user = next(
            (m for m in reversed(messages) if m.get("role") == "user"),
            None,
        )
        if last_user:
            content = last_user.get("content", "")
            if isinstance(content, str) and content.strip():
                summary = summarize_text(content)

        # 3. 与盘上 meta 合并
        meta = _read_meta_from_file(target_path)
        source = (meta or {}).get("source")
        if source is None and target_sid == self._session_id:
            source = self._meta.get("source")
        if summary and source == "subagent" and not summary.startswith("[subagent] "):
            summary = f"[subagent] {summary}"

        if meta is None:
            if not summary:
                return ""  # 空会话 + 文件不在盘上 → 不造 0 长度文件
            self.update_meta(target_sid, summary=summary)
            return summary

        if not summary:
            summary = meta.get("summary") or "empty_session"
        self.update_meta(target_sid, summary=summary)
        return summary

    def update_meta(self, sid: str | None = None, *, create: bool = True, **kwargs: Any) -> None:
        """更新指定会话的内嵌 meta 字段。

        与模块级 update_session_meta 对齐：会话文件不存在时默认自动创建
        （惰性文件场景，如 subagent 预生成 sid 后立即标记 source），
        并同步内存 _meta，避免后续 save_context 覆盖。

        Args:
            sid: 目标会话（None = 当前会话）
            create: 文件不存在时是否**创建文件**。False = 只更新内存 meta
                （当前会话）或直接放弃（非当前会话）——用于「标记类」写入
                （token_usage、source 标记）不该给空会话凭空造出一个
                0 长度会话文件。
        """
        sid = sid or self._session_id
        path = _session_path(sid)
        meta = _read_meta_from_file(path)
        if meta is None:
            if not create:
                if sid == self._session_id:
                    self._meta.update(kwargs)
                    self._meta["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                return
            meta = _default_meta(sid)
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(meta, ensure_ascii=False) + "\n")
            except Exception:
                return
        meta.update(kwargs)
        meta["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _write_meta_to_file(path, meta)
        if sid == self._session_id:
            self._meta = meta
