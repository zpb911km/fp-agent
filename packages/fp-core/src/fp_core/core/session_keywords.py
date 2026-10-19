"""会话关键词索引：KL 散度管线（零 API 调用）。

为什么需要它
------------
会话标题只是「最后一条 user 消息的前 20 字」，会话一多就退化成
「推送 / 提交 / ok」这类无信息噪声。本模块给每个会话补 3 个**区分性**
关键词——用 KL 贡献 ``P_d·log(P_d/P_C)`` 衡量「这个词在这个会话里比在
日常语料里突出多少」。

三条铁律
--------
1. **零 API**：纯本地统计，不调用任何 LLM。
2. **列表渲染零计算**：`meta["kw"]` 是预先算好的快照，列表只读不算是；
   缺失就留空（``list_sessions()`` 在 905 个会话的循环里，实算会让毫秒
   级列表变成秒级）。
3. **P_C 不做全量重算**：按天分桶存语料统计，背景模型 = 合并最近
   ``WINDOW_DAYS`` 天（毫秒级）。日常新会话**不写桶**（只读背景快照算自己），
   桶由低频的 ``rebuild_all()`` 一次性刷新——背景是「日常语料」，滞后几天
   对打分的影响远小于每次落盘重写桶的代价。

存储布局
--------
``{SESSIONS_DIR}/.kw/``
  - ``df-YYYYMMDD.json``  当天语料统计 ``{"tc": {词: 频次}, "gdf": {词: 文档数}, "n": 会话数}``
  - ``state.json``        ``{"kw_v": 版本, "sup": [抑制词], "rebuilt_at": ...}``

词形说明
--------
只取 CJK 连续段（``[\\u4e00-\\u9fff]``）→ 代码块天然排除；
分词按时**行切分后**再喂 jieba，否则行尾行首会粘连出假词。
"""

from __future__ import annotations

import collections
import datetime
import json
import math
import os
import re
import threading
from collections.abc import Iterable
from typing import Any, cast

from .. import config

# ── 参数（源于 905 会话离线实验，勿随意改动） ──────────────
KW_VERSION = 1
"""索引版本；管线语义变化时 +1，用于判定旧 kw 是否过期。"""

WINDOW_DAYS = 21
"""背景语料窗口（天）。全局 IDF 在「工作冲刺期」会失效，故用局部窗口。"""

MU = 1500.0
"""Dirichlet 平滑强度：让低频词不至于因 ``p_c`` 极小而被夸大。"""

DF_MAX = 0.10
"""文档频次上限：出现在 >10% 会话里的词是背景噪声，整词剔除。"""

MIN_DF = 3
"""文档频次下限：只出现过 1~2 次的词多半是错字或专名碎片。"""

TOP_K = 3
"""写入 meta 的词数（终端宽度有限；webui 宽屏自行截断展示 5 词）。"""

USER_WEIGHT = 2
"""user 正文加权——用户自己打字的内容最能代表「这个会话在干嘛」。"""

SUPPRESS_RATIO = 0.02
"""二遍抑制：某词出现在 >2% 会话的 top 列表里 → 说明它对**区分**没贡献。"""

_DIR_NAME = ".kw"
_STATE_FILE = "state.json"

# 注入型前缀：不是用户真实意图，参与统计会污染关键词
_INJECTED_PREFIXES = ("[背景信息]", "⁂", "[子图]", "[subagent]", "[任务]", "[铃声]")

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_CODE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*|/[A-Za-z0-9_./\-]{4,}")
_SPLIT_RE = re.compile(r"[。！？；，、,.!?;:：\s]+")

# 高频虚词：即使 KL 靠平滑把它们顶上来也无意义
_CJK_STOP = frozenset([
    "的",
    "了",
    "是",
    "我",
    "你",
    "他",
    "她",
    "它",
    "们",
    "这",
    "那",
    "就",
    "都",
    "也",
    "还",
    "又",
    "很",
    "更",
    "最",
    "和",
    "与",
    "或",
    "但",
    "而",
    "这个",
    "那个",
    "什么",
    "怎么",
    "在",
    "有",
    "没",
    "要",
    "会",
    "被",
    "把",
    "给",
    "对",
    "从",
    "到",
    "说",
    "看",
    "做",
    "用",
    "想",
    "让",
    "已经",
    "现在",
    "时候",
    "问题",
    "东西",
    "这样",
    "那样",
    "然后",
    "但是",
    "而且",
    "或者",
    "不是",
    "就是",
    "还是",
    "只是",
    "这里",
    "那里",
    "我们",
    "你们",
    "他们",
    "自己",
    "应该",
    "需要",
    "可能",
    "知道",
    "觉得",
    "感觉",
    "直接",
    "一下",
    "一点",
    "一样",
    "一些",
    "进行",
    "出现",
    "发现",
    "使用",
    "没有",
    "地",
    "得",
    "着",
    "过",
    "如果",
    "因为",
    "所以",
    "虽然",
    "不过",
    "并且",
    "以及",
    "例如",
    "比如",
    "包括",
    "通过",
    "关于",
    "对于",
    "根据",
    "按照",
    "由于",
    "于是",
    "之后",
    "之前",
    "可以",
    "一个",
    "请你",
    "帮我",
    "看看",
])

_jieba_mod: Any = None
_jieba_lock = threading.Lock()


def _jieba() -> Any:
    """惰性加载 jieba；不可用时返回 None（退化到 bigram 切分）。

    jieba 是可选依赖（``fp-core[kw]``）——不让关键词功能成为硬依赖。
    """
    global _jieba_mod
    if _jieba_mod is not None:
        return _jieba_mod or None
    with _jieba_lock:
        if _jieba_mod is None:
            try:
                import jieba  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs]

                jb: Any = jieba
                jb.setLogLevel(60)  # 静音：避免首载时往终端吐加载日志
                _jieba_mod = jb
            except Exception:
                _jieba_mod = False
    return _jieba_mod or None


def warmup() -> None:
    """预热分词器（约 1s）。后台线程调用，别在会话结束路径上首次触发。"""
    _jieba()


# ── 分词 ──────────────────────────────────────────────


def _cut(seg: str) -> Iterable[str]:
    jb = _jieba()
    if jb is not None:
        yield from jb.cut(seg)
        return
    # 降级：CJK 连续串做 bigram（无词典，质量下降但不断功能）
    for run in _CJK_RUN.findall(seg):
        if len(run) == 2:
            yield run
        for i in range(len(run) - 1):
            yield run[i : i + 2]


def tokenize(texts: Iterable[Any], weight: int = 1) -> collections.Counter[str]:
    """正文 → 词频表。按行切分后再分词（防跨行粘连假词）。"""
    cnt: collections.Counter[str] = collections.Counter()
    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        for line in text.split("\n"):
            line = _CODE_RE.sub(" ", line)  # 标识符/路径先进噪声，避免切碎
            for seg in _SPLIT_RE.split(line):
                if not seg:
                    continue
                for word in _cut(seg):
                    word = word.strip()
                    if len(word) >= 2 and _CJK_RUN.fullmatch(word) and word not in _CJK_STOP:
                        cnt[word] += weight
    return cnt


def is_injected(content: str) -> bool:
    """该 user 消息是否属于系统注入（不参与关键词统计）。"""
    return content.startswith(_INJECTED_PREFIXES)


# ── 会话文件解析 ───────────────────────────────────────


def read_session(path: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """读会话文件 → (meta, messages)。坏行跳过，不抛异常。"""
    meta: dict[str, Any] = {}
    msgs: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("__meta__"):
                    meta = obj
                else:
                    msgs.append(obj)
    except OSError:
        return {}, []
    return meta, msgs


def session_tokens(msgs: list[dict[str, Any]], skip_before: int = 0) -> collections.Counter[str]:
    """消息列表 → 词频表。

    ``skip_before``: fork 会话只统计分叉点之后的内容——共享前缀是
    「关键词扎堆」的根因（同 fork 组内 Jaccard 0.372，非 fork 仅 0.080）。
    """
    body = msgs[skip_before:]
    users = [
        m.get("content")
        for m in body
        if m.get("role") == "user" and isinstance(m.get("content"), str) and not is_injected(m["content"])
    ]
    assts = [m.get("content") for m in body if m.get("role") == "assistant"]
    cnt = tokenize(users, USER_WEIGHT)
    cnt.update(tokenize(assts, 1))
    return cnt


# ── 按天 DF 桶 ────────────────────────────────────────


def kw_dir(sessions_dir: str | None = None) -> str:
    return os.path.join(sessions_dir or config.SESSIONS_DIR, _DIR_NAME)


def _bucket_path(day: datetime.date, sessions_dir: str | None = None) -> str:
    return os.path.join(kw_dir(sessions_dir), f"df-{day.strftime('%Y%m%d')}.json")


def _session_day(meta: dict[str, Any]) -> datetime.date | None:
    stamp = meta.get("updated") or meta.get("created") or ""
    try:
        return datetime.datetime.strptime(str(stamp)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def save_bucket(
    day: datetime.date,
    tc: collections.Counter[str],
    gdf: collections.Counter[str],
    n: int,
    sessions_dir: str | None = None,
) -> bool:
    """写当天桶（覆盖）。"""
    os.makedirs(kw_dir(sessions_dir), exist_ok=True)
    path = _bucket_path(day, sessions_dir)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"tc": dict(tc), "gdf": dict(gdf), "n": int(n)},
                f,
                ensure_ascii=False,
            )
        return True
    except OSError:
        return False


def load_background(
    day: datetime.date,
    sessions_dir: str | None = None,
    window: int = WINDOW_DAYS,
) -> tuple[collections.Counter[str], int, collections.Counter[str], int]:
    """合并 ``[day-window, day]`` 内所有桶 → (tc, T, gdf, N)。

    只取**过去**的窗口（在线场景看不到未来）。桶缺失就跳过——冷启动时
    背景为空，``score()`` 会退化为纯词频，仍然可用。
    """
    tc: collections.Counter[str] = collections.Counter()
    gdf: collections.Counter[str] = collections.Counter()
    n = 0
    for delta in range(window + 1):
        path = _bucket_path(day - datetime.timedelta(days=delta), sessions_dir)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        tc.update(data.get("tc") or {})
        gdf.update(data.get("gdf") or {})
        n += int(data.get("n") or 0)
    return tc, sum(tc.values()), gdf, n


# ── 打分 ──────────────────────────────────────────────


def score(
    cnt: collections.Counter[str],
    tc: collections.Counter[str],
    total: int,
    gdf: collections.Counter[str],
    n_sessions: int,
    top_k: int = TOP_K,
    suppress: frozenset[str] | set[str] | None = None,
) -> list[str]:
    """KL 贡献排序取 top_k。空背景时退化为纯词频排序。"""
    doc_len = sum(cnt.values())
    if not doc_len:
        return []
    scored: dict[str, float] = {}
    for word, freq in cnt.items():
        if suppress and word in suppress:
            continue
        if n_sessions >= 10:
            df = gdf.get(word, 0)
            if df < MIN_DF or df > DF_MAX * n_sessions:
                continue
        # 无背景时平滑项退化为常数，排序等价于纯词频
        p_c = max(tc.get(word, 0) / total, 1e-9) if total > 0 else 1.0
        p_d = (freq + MU * p_c) / (doc_len + MU)
        scored[word] = p_d * math.log(p_d / p_c)
    ranked = sorted(scored.items(), key=lambda kv: -kv[1])
    return [w for w, _ in ranked[:top_k]]


# ── 索引状态（抑制词表） ──────────────────────────────


def load_state(sessions_dir: str | None = None) -> dict[str, Any]:
    path = os.path.join(kw_dir(sessions_dir), _STATE_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return cast(dict[str, Any], data)
    except Exception:
        pass
    return {}


def save_state(state: dict[str, Any], sessions_dir: str | None = None) -> bool:
    os.makedirs(kw_dir(sessions_dir), exist_ok=True)
    try:
        with open(os.path.join(kw_dir(sessions_dir), _STATE_FILE), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        return True
    except OSError:
        return False


def _suppress_set(sessions_dir: str | None = None) -> frozenset[str]:
    sup = load_state(sessions_dir).get("sup")
    if isinstance(sup, list):
        return frozenset(str(w) for w in cast(list[Any], sup))
    return frozenset()


# ── 写回 meta ─────────────────────────────────────────


def _write_meta_header(path: str, meta: dict[str, Any]) -> bool:
    """重写会话文件第一行（meta header），正文原样保留。"""
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
            f.writelines(lines[1:])
        return True
    except OSError:
        return False


def _fork_skip(meta: dict[str, Any]) -> int:
    """fork 会话应跳过的前缀消息数。"""
    fork = meta.get("fork_from")
    if isinstance(fork, dict):
        try:
            raw = cast(dict[str, Any], fork).get("msg_count")
            return max(0, int(raw or 0))
        except (TypeError, ValueError):
            return 0
    return 0


def compute_from_messages(
    meta: dict[str, Any],
    msgs: list[dict[str, Any]],
    sessions_dir: str | None = None,
) -> list[str]:
    """(meta, 消息列表) → 关键词列表。内存路径，不读盘。

    会话落盘路径（``save_and_summarize``）手上已有完整消息，用这个省一次
    全文件读取。
    """
    if not meta or not msgs:
        return []
    day = _session_day(meta) or datetime.date.today()
    cnt = session_tokens(msgs, skip_before=_fork_skip(meta))
    if not cnt:
        return []
    tc, total, gdf, n = load_background(day, sessions_dir)
    return score(cnt, tc, total, gdf, n, TOP_K, _suppress_set(sessions_dir))


def compute(path: str, sessions_dir: str | None = None) -> list[str]:
    """单会话文件 → 关键词列表（不写盘）。"""
    meta, msgs = read_session(path)
    return compute_from_messages(meta, msgs, sessions_dir)


def update_session(sid: str, path: str, sessions_dir: str | None = None) -> list[str] | None:
    """算并写回 ``meta.kw``。失败静默返回 None（关键词是增强，不能拖垮主流程）。"""
    try:
        meta, msgs = read_session(path)
        if not meta or not msgs:
            return None
        kws = compute_from_messages(meta, msgs, sessions_dir)
        meta["kw"] = kws
        meta["kw_v"] = KW_VERSION
        if _write_meta_header(path, meta):
            return kws
    except Exception:
        return None
    return None


# ── 全量重建（一次性回填 / 手动维护） ──────────────────


def rebuild_all(
    sessions_dir: str | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    """全量重扫：重建按天桶 + 抑制词表 + 每个会话的 ``meta.kw``。

    成本约 25s/900 会话（不含写盘）。只在首次回填或手动维护时跑。
    返回统计字典。
    """
    root = sessions_dir or config.SESSIONS_DIR
    warmup()
    # 1. 第一遍：扫全部会话，累积按天桶 + 全局文档频次
    day_tc: dict[datetime.date, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    day_gdf: dict[datetime.date, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    day_n: collections.Counter[datetime.date] = collections.Counter()
    docs: list[tuple[str, str, dict[str, Any], collections.Counter[str]]] = []

    for name in sorted(os.listdir(root)):
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(root, name)
        meta, msgs = read_session(path)
        if not meta or not msgs:
            continue
        sid = meta.get("id") or name[:-6]
        day = _session_day(meta) or datetime.date.today()
        cnt = session_tokens(msgs, skip_before=_fork_skip(meta))
        docs.append((sid, path, meta, cnt))
        day_tc[day].update(cnt)
        for word in cnt:
            day_gdf[day][word] += 1
        day_n[day] += 1
        if progress and len(docs) % 100 == 0:
            progress(f"扫描 {len(docs)} 个会话…")

    for day in day_tc:
        save_bucket(day, day_tc[day], day_gdf[day], day_n[day], root)

    # 2. 第二遍：逐会话打分（此时桶已就绪，背景窗口可用）
    n_sessions = len(docs)
    first_pass: dict[str, list[str]] = {}
    for sid, _path, meta, cnt in docs:
        day = _session_day(meta) or datetime.date.today()
        tc, total, gdf, _n = load_background(day, root)
        first_pass[sid] = score(cnt, tc, total, gdf, n_sessions, top_k=14)

    # 3. 二遍抑制：跨会话重复出现的 top 词对区分没贡献
    appear: collections.Counter[str] = collections.Counter()
    for words in first_pass.values():
        for word in set(words):
            appear[word] += 1
    threshold = max(8, int(SUPPRESS_RATIO * n_sessions))
    suppress = sorted(w for w, c in appear.items() if c > threshold)

    sup_set = frozenset(suppress)
    written = 0
    empty = 0
    for _sid, path, meta, cnt in docs:
        day = _session_day(meta) or datetime.date.today()
        tc, total, gdf, _n = load_background(day, root)
        kws = score(cnt, tc, total, gdf, n_sessions, TOP_K, sup_set)
        meta["kw"] = kws
        meta["kw_v"] = KW_VERSION
        if _write_meta_header(path, meta):
            written += 1
        if not kws:
            empty += 1
        if progress and written % 100 == 0:
            progress(f"写回 {written} 个会话…")

    state = load_state(root)
    state.update({
        "kw_v": KW_VERSION,
        "sup": suppress,
        "rebuilt_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sessions": n_sessions,
    })
    save_state(state, root)
    return {
        "sessions": n_sessions,
        "written": written,
        "empty": empty,
        "suppress": len(suppress),
        "empty_ratio": round(empty / n_sessions, 4) if n_sessions else 0.0,
    }


def needs_rebuild(sessions_dir: str | None = None, stale_hours: int = 24) -> bool:
    """启动时判断：索引是否落后到该滚桶了（非阻塞调用方自行决定后台执行）。"""
    state = load_state(sessions_dir)
    if state.get("kw_v") != KW_VERSION:
        return True
    stamp = state.get("rebuilt_at") or ""
    if not stamp:
        return True
    try:
        last = datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    return (datetime.datetime.now() - last).total_seconds() > stale_hours * 3600


def main(argv: list[str] | None = None) -> int:
    """手动入口：``python -m fp_core.core.session_keywords rebuild|show``。"""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    action = args[0] if args else "rebuild"

    def _console(msg: str) -> None:
        print(msg, flush=True)

    if action == "rebuild":
        stats = rebuild_all(progress=_console)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0
    if action == "show":
        root = config.SESSIONS_DIR
        for name in sorted(os.listdir(root))[-20:]:
            if not name.endswith(".jsonl"):
                continue
            meta, _ = read_session(os.path.join(root, name))
            if meta:
                print(f"{meta.get('id')}  {(meta.get('summary') or '')[:30]}  →  {' · '.join(meta.get('kw') or [])}")
        return 0
    print("用法: rebuild | show")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
