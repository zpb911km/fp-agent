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
import threading
import time
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
    """重写会话文件的第一行（meta header），正文原样保留。

    **原子写**（tmp + ``os.replace``）：旧实现 ``open(path,"w")`` 截断后回填，
    中途崩溃会让首行残缺 → 整个会话从列表静默消失。全量重建会写 941 个文件，
    不能赌「不会崩」。
    """
    if not os.path.exists(path):
        return False
    tmp = f"{path}.metatmp"
    try:
        content = json.dumps(meta, ensure_ascii=False)
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content + "\n")
            f.writelines(lines[1:])
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return False


SUMMARY_LIMIT = 20
"""summary 字符上限（**硬**上限）。

summary 是列表里一行一个的「路标」，不是摘要——它必须短到能一眼扫过。
20 字是实测的「一句完整话」长度：截到 10 字会砍掉宾语（``请你找到webui的``、
``我想修改一下简历``），正好丢掉最能认人的部分。所以取用户**最后一条**消息
（比首条开场指令有记忆点，跨会话也不易重复）截到 20 字。
"""

# 注入型前缀：这些开头的 user 消息是系统塞进来的，不是用户意图
_SUMMARY_INJECTED = ("[背景信息]", "⁂", "[子图]", "[subagent]", "[任务]", "[铃声]", "[task]", "【")

SUMMARY_LOCKED = "summary_locked"
"""meta 键：摘要已被**人工重命名**锁定（见 ``SessionManager.set_summary``）。

为什么需要一把锁：summary 是自动派生的（跟随最后一条 user 消息），
``save_context`` / ``save_message`` / ``save_and_summarize`` 三条落盘路径都会
重写它。人工改完名字若不锁，用户发下一条消息就会静默变回派生值——「改了但没改」，
比不支持改名更糟。锁存于会话 meta（随文件走），故重启/reload 后依然生效。
"""


def _clip(text: str) -> str:
    """截到 ``SUMMARY_LIMIT`` 字，被截断时末位补省略号。

    为什么不裸切：``packages/fp-core.md`` 裸切成 ``packages/f`` 会被误读成
    真实文件名；``…`` 明确告知"这里还有内容"。
    """
    if len(text) <= SUMMARY_LIMIT:
        return text
    return text[: SUMMARY_LIMIT - 1] + "…"


def _strip_injection(text: str) -> str:
    """从注入型消息里剥出残留的**用户意图正文**（剥不出返回 ``""``）。

    为什么不能整条跳过：真实数据里注入块和用户指令**在同一条消息里**——
    ``[背景信息]\\n<背景>\\n\\n[任务]\\n<用户指令>`` 与 ``【经理派遣·x】<用户指令>``。
    按前缀整条丢弃会把用户原话一起扔掉，实测 77 个会话因此拿不到 summary，
    只能沿用旧值（70 个至今显示着上一版的 kw[0]）。

    纯通知型（``⁂[peer_ring] 【铃声】...``）剥完为空，返回 ``""`` 继续往前找。
    """
    s = text.strip()
    if not s:
        return ""
    # [背景信息] 块之后可能跟 [任务]（真实指令）或 [子图]（子图任务）
    if s.startswith(("[背景信息]", "[子图]")):
        for marker in ("[任务]", "[子图]"):
            if marker in s:
                body = s.rsplit(marker, 1)[1].strip()
                if body:
                    return body
        return ""
    # 【经理派遣·sidebar·接替2】<用户指令>：标签后即正文；
    # 【铃声】peer:xxx 在找你… 是纯通知（标签本身已是完整语义，正文只是路由提示）
    if s.startswith("【"):
        idx = s.find("】")
        if idx < 0:
            return ""
        label = s[1:idx].strip()
        body = s[idx + 1 :].strip()
        if not body or label.startswith("铃声") or body.startswith(("[铃声]", "⁂")):
            return ""
        return body
    if s.startswith(("⁂", "[铃声]", "[subagent]", "[task]", "[任务]")):
        return ""
    return s


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    """取最后一条**可用的**用户意图正文（单行化）。

    逆序扫描 user 消息，每条先经 ``_strip_injection`` 剥注入块：剥出正文就用
    （末条常是 ``⁂[job_killed]`` / ``[铃声]`` 这类系统补齐，无正文则继续往前）；
    整段会话都没有用户正文时（实测 20 个 peer_ring / 无 user 的会话）回退到
    **首条非空 assistant 正文**——它通常复述了任务，比沿用旧摘要更有信息量。
    """
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if not isinstance(content, str):
            continue
        body = _strip_injection(content)
        if body:
            return body.replace("\n", " ")
    for m in messages:
        if m.get("role") != "assistant":
            continue
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip().replace("\n", " ")
    return ""


def derive_summary(kws: list[str] | None, fallback: str = "") -> str:
    """由**最后一条 user 消息**派生 summary，截到 ``SUMMARY_LIMIT`` 字。

    为什么用末条 user 而不是关键词：kw 抽的是「会话内突出」的统计，取 kw[0]
    当摘要在 941 个真实会话上实测 39.5% 撞车（「错误」×13、「版本」×11），
    且抽出来常是「对冲」「示波器」「抓不住」这类分词碎片，人无法据此认出
    是哪次会话。末条 user 是用户亲手写的原话，天然有记忆点，跨会话也不易重复。

    ``kws`` 参数仅为签名兼容保留，不参与计算（subagent_plugin 等调用方仍传）。

    传进来的 ``fallback`` 应已由 ``_strip_injection`` / ``_last_user_text`` 剥过
    注入块；仍留 ``_SUMMARY_INJECTED`` 前缀守卫作兜底，命中时返回 ""，由调用方
    沿用盘上旧摘要。
    """
    clean = (fallback or "").strip().replace("\n", " ")
    for prefix in _SUMMARY_INJECTED:
        if clean.startswith(prefix):
            return ""
    return _clip(clean)


# ── 语料桶自愈 ────────────────────────────────────────
_BUCKET_LOCK = threading.Lock()
_BUCKET_STATE: dict[str, float] = {"t": 0.0}
_BUCKET_INTERVAL = 600.0
"""刷新「今天」语料桶的最小间隔（秒）。

桶原本只由手动 ``rebuild_all()`` 生成——实测 2026-10-21 起断供 43 天，
背景窗口取空后 KL 打分静默退化成纯词频，抽出的全是「哪些/真实/实际」
这类虚词。这里让会话结束时顺手补一格：10 分钟一次、后台线程、不阻塞保存。
``load_background`` 的「回退最近非空桶」是第一道止血，本机制是让它不再流血。
"""


def _maybe_refresh_bucket() -> None:
    """节流刷新当天语料桶（后台线程，任何异常静默）。"""
    now = time.time()
    with _BUCKET_LOCK:
        if now - _BUCKET_STATE.get("t", 0.0) < _BUCKET_INTERVAL:
            return
        _BUCKET_STATE["t"] = now

    def _work() -> None:
        try:
            from datetime import date

            from .session_keywords import refresh_day_bucket

            refresh_day_bucket(date.today())
        except Exception:
            pass

    with contextlib.suppress(Exception):
        threading.Thread(target=_work, daemon=True).start()


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
    first_asst = ""
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
                role = d.get("role")
                content = d.get("content")
                if not isinstance(content, str) or not content.strip():
                    continue
                # 与 _last_user_text 同一套剥离：注入块里可能藏着真实指令
                if role == "user":
                    body = _strip_injection(content)
                    if body:
                        last_user = body
                elif role == "assistant" and not first_asst:
                    first_asst = content
    except Exception:
        return True, "", 0
    return has_msgs, last_user or first_asst, count


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
    _token_tracker: Any

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
        # token 追踪器由 Agent 在构造后 bind（此处 SessionManager 先于它存在）
        self._token_tracker = None

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

    # ── token 追踪器绑定 ─────────────────────────

    def bind_token_tracker(self, tracker: Any) -> None:
        """绑定 Agent 的 TokenTracker，此后会话切换时自动对齐其累计值。

        为什么由 SessionManager 负责对齐：``token_usage`` 是**按会话**的累计
        快照，而 tracker 是**按进程**的单例。若不绑定，``/new`` 后新会话会
        继承旧会话的累计（存多），``/resume`` 切到别的会话则带着当前会话的
        数字（串味）。切换点只有 SessionManager 知道，所以在它这里同步最不易漏。
        """
        self._token_tracker = tracker
        self._sync_token_tracker()

    def _sync_token_tracker(self) -> None:
        """把 tracker 对齐到当前会话的 meta（无用量的会话 → 归零）。"""
        if self._token_tracker is None:
            return
        self._token_tracker.restore(self._meta.get("token_usage"))

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
        if not summary and not meta.get(SUMMARY_LOCKED):
            # 回填：用净化后的末条 user（kw 不再参与摘要）
            # 锁定（人工重命名过）时不回填——名字是用户的，派生值没资格覆盖它，
            # 哪怕它此刻为空（空名字是用户的表达，不是缺陷）。
            summary = derive_summary(None, last_user)
            if summary:
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
        self._sync_token_tracker()  # token 累计随之对齐到目标会话
        return True

    def create_session(self, sid: str | None = None) -> str:
        """创建新会话并切换过去（惰性文件创建）。

        Args:
            sid: 可指定 sid（如 subagent 预生成），None 时自动生成
        """
        self._session_id = self._allocate_session(sid)
        self._meta = self._load_meta_from_session()
        _register_current_session(self._session_id)  # 同步进程级「当前会话」
        self._sync_token_tracker()  # 新会话无用量 → tracker 归零，不继承旧会话
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
        self._sync_token_tracker()  # 清空会话 = 无用量，tracker 同步归零
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
            self._sync_token_tracker()
            return True
        # 没有历史会话 → 分配新会话 ID（惰性文件创建）
        self._session_id = self._allocate_session()
        self._meta = self._load_meta_from_session()
        self._sync_token_tracker()
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
        """**仅在摘要为空时**用最后一条 user 消息兜底，不再逐轮覆写。

        策略：**每轮跟随最后一条非系统注入的 user 消息**（``_last_user_text``）。
        选末条而非首条：末条是用户最后一次亲口交代的需求，天然有记忆点、
        跨会话不易撞车；首条常是「读一下docs」这类空壳指令。
        末条是系统注入或算不出内容时不动旧摘要。

        ``summary_locked``（人工重命名，见 ``set_summary``）为真时整体让路：
        派生值绝不能覆盖用户亲手起的名字。
        """
        if self._meta.get(SUMMARY_LOCKED):
            return
        summary = derive_summary(None, _last_user_text(messages))
        if not summary:
            return
        if self._meta.get("source") == "subagent" and not summary.startswith("[subagent] "):
            summary = f"[subagent] {summary}"
        self._meta["summary"] = summary

    def save_context(self, context: list[dict[str, Any]], meta_updates: dict[str, Any] | None = None) -> None:
        """将完整上下文写入文件。context 应为 to_serializable() 的输出（无 system prompt）。

        Args:
            context: 待落盘的完整上下文（system 消息会被过滤）
            meta_updates: 与正文**同一次写入**合并进 meta 的字段（如
                ``{"token_usage": ...}``）。走这条路径而不是事后再调
                ``update_meta``，可以保证「一盘一写」——每轮问答落盘时
                token 用量随之增量更新，也不会出现 meta 与正文不同步的窗口。
        """
        # 防御性过滤：防止误传入 system 消息
        context = [m for m in context if m.get("role") != "system"]
        path = self._session_path()

        if not context:
            return

        lines = [json.dumps(msg, ensure_ascii=False) for msg in context]

        if meta_updates:
            self._meta.update(meta_updates)
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

    def save_and_summarize(
        self,
        messages: list[dict[str, Any]],
        session_id: str | None = None,
        meta_updates: dict[str, Any] | None = None,
    ) -> str:
        """保存会话上下文并生成摘要（统一入口）。

        所有会话切换/退出路径都应调用此方法，确保摘要生成逻辑一致。
        摘要策略：**跟随最后一条 user 消息**——取最后一条非系统注入的 user
        正文，截到 ``SUMMARY_LIMIT``(20) 字。kw 只作检索指纹，不参与摘要。
        subagent 会话（meta.source == "subagent"）自动加 "[subagent] " 前缀。

        不变量：
          - **消息为空且文件不在盘上 → 完全 no-op**（不创建 0 长度会话文件）。
            空转的进程退出、`/new` 前的落盘都不该留下占位文件——这正是
            会话列表被 0 长度记录刷屏的主因。
          - **已有摘要不被空结果抹掉**：算不出新摘要时沿用盘上旧摘要，
            两者皆空才写 `empty_session` 标记（文件已存在时）。

        Args:
            messages: to_serializable() 输出的非 system 消息列表
            session_id: 目标会话 ID（None 表示当前会话）
            meta_updates: 与正文同一次写入合并进 meta 的字段（如
                ``{"token_usage": ...}``，退出时结算用）。仅对当前会话生效
                （``save_context`` 只写当前 sid，切走旧会话时该字段无效）。

        Returns:
            摘要文本；无可保存内容且文件不在盘上时返回 ""
        """
        target_sid = session_id or self._session_id
        target_path = _session_path(target_sid)

        # 0. 空会话 + 文件不存在 → 完全 no-op，且必须在刷 kw **之前**拦。
        #    _refresh_keywords 里的 update_meta 会创建 meta 文件，一旦先跑完，
        #    下面再读 meta 就不是 None 了，于是走进"已有文件"分支写进
        #    `empty_session` 标记——空转进程退出就留下一条空会话记录。
        if not messages and not os.path.exists(target_path):
            return ""

        # 1. 持久化消息（空 context 时 save_context 是 no-op，不建文件）
        self.save_context(messages, meta_updates=meta_updates)

        # 2. 先刷关键词——summary 由 kw 派生，必须排在它前面
        self._refresh_keywords(target_sid, messages)

        # 3. 摘要跟随最后一条 user（kw 只作检索指纹，不参与摘要）
        #    人工重命名过（summary_locked）→ 派生值让路：切换/退出路径不许
        #    把用户亲手起的名字改回自动摘要（save_context 已刷 kw，此处只需返回）。
        meta = _read_meta_from_file(target_path)
        if (meta or {}).get(SUMMARY_LOCKED):
            return str((meta or {}).get("summary") or "")

        summary = derive_summary(None, _last_user_text(messages))

        # 4. 与盘上 meta 合并（meta 已在上面读入）
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

    def _refresh_keywords(self, sid: str, messages: list[dict[str, Any]]) -> list[str] | None:
        """刷新会话关键词（``meta.kw``）并返回它——挂在落盘/切换这条**低频**路径上。

        为什么不在 ``list_sessions()`` 里算：那里是 900+ 会话的循环，实算会
        把毫秒级列表拖成秒级。列表永远只读现成字段，缺失即留空。
        单会话成本约 18ms（900 会话全量回放实测）。

        返回 kw 是为了让 ``save_and_summarize`` 能立刻用它派生 summary——
        summary 与 kw 必须同源，否则又是两套漂移的规则。

        关键词是**增强**功能：任何异常都静默吞掉，不能让会话保存失败。
        """
        try:
            from .session_keywords import KW_VERSION, compute_from_messages

            meta = self._meta if sid == self._session_id else (_read_meta_from_file(_session_path(sid)) or {})
            kws = compute_from_messages(meta, messages)
            self.update_meta(sid, kw=kws, kw_v=KW_VERSION)
            _maybe_refresh_bucket()  # 桶链自愈：不靠手动 rebuild_all
            return kws
        except Exception:
            return None

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

    def set_summary(self, sid: str, summary: str) -> str | None:
        """**人工重命名**会话（任意 sid）：写 ``meta.summary`` 并加锁，返回生效文本。

        与 `update_meta(summary=...)` 的区别（这是它与「顺手写个字段」的分水岭）：

          1. **加锁**：置 ``meta[SUMMARY_LOCKED]``，让三条自动派生路径
             （`_refresh_summary` / `save_and_summarize` / 列表回填）全体让路。
             不加锁的名字会被**下一条用户消息**立刻改回派生值——见
             ``SUMMARY_LOCKED`` 的注释。
          2. **不动 ``updated``**：改名不该让会话在列表里跳到最前面
             （列表排序键是 updated，与列表回填写盘时同一纪律）。
          3. **不创建文件**：惰性会话（当前会话尚未落盘）只改内存 meta，
             名字随首条消息一起落盘；非当前会话且文件不存在 → `None`（无此会话）。
          4. **规范化**：空白折叠成单空格（换行会破坏列表一行一条的路标形态），
             超 ``SUMMARY_LIMIT`` 截断加省略号——与自动派生同一上限，前端据此
             渲染 maxlength，不在两处各写一个数字。

        Args:
            sid: 目标会话 ID（任意会话，不限于当前）
            summary: 新名字；**空串 = 撤销人工命名**，回到自动派生（立即按
                文件末条 user 消息重算，避免界面闪一次「(无摘要)」）

        Returns:
            最终生效的摘要文本；会话不存在 / 写盘失败 → `None`
        """
        sid = (sid or "").strip()
        # 防路径穿越：sid 直接来自 HTTP path/CLI 参数，不许带分隔符
        if not sid or "/" in sid or "\\" in sid or sid in (".", ".."):
            return None

        path = _session_path(sid)
        on_disk = os.path.exists(path)
        meta = _read_meta_from_file(path) if on_disk else None
        is_current = sid == self._session_id
        if meta is None:
            if not is_current:
                return None  # 既不在盘上也不是当前会话 → 无此会话
            meta = dict(self._meta)
        elif is_current:
            # 内存优先：标记类写入（source/token_usage，create=False）可能还没落盘
            merged = dict(meta)
            merged.update(self._meta)
            meta = merged

        text = _clip(" ".join(str(summary or "").split()))
        if text:
            meta["summary"] = text
            meta[SUMMARY_LOCKED] = True
        else:
            meta.pop(SUMMARY_LOCKED, None)
            meta["summary"] = derive_summary(None, _scan_messages(path)[1]) if on_disk else ""
            text = str(meta.get("summary") or "")

        if on_disk and not _write_meta_to_file(path, meta):
            return None  # 写盘失败 → 内存也不动，避免「显示改了、盘上没改」
        if is_current:
            self._meta = meta
        return text
