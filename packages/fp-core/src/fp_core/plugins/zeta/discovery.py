"""Zeta 发现与身份 —— 邻居表条目从哪来、实例如何占坑。

两块内容：
1. **DiscoveryBackend**：发现后端（可插拔）。MVP 提供 :class:`SharedDirBackend`
   （同机共享目录）；跨机换 rendezvous 实现即可，领域模型不感知差别。
   TODO(n12) 跨网络发现后端（已存档暂缓）：RegistryBackend（VPS rendezvous，HTTP PUT/GET 卡）
   或 StaticConfigBackend（写死邻居地址）。方案见记忆 zeta_peer_protocol。
2. **InstanceRegistry**：一 workspace 多实例登记（§1.5 修订版）。以
   ``.fp/zeta/instances/`` 下每实例一个登记文件替代单实例锁——同 workspace 可并行
   多实例，天然共享记忆/任务。身份锚点是会话 **sid**（非 cwd）；``workspace`` 只是
   项目归属属性（路由键）。

**reload 语义（本模块最容易踩的坑）**：登记的 ``pid`` 只回答「进程还在不在」，答不了
「还是不是刚才那个**化身**」。POSIX reload 是 ``os.execve`` **映像替换、PID 不变**，
旧化身不会执行任何 shutdown 钩子（进程原地重生，没有"退出"可挂）——于是登记残留、
``pid`` 判活为真、sid 又稳定 ⇒ 派生同名 ⇒ 新化身把自己认成别人、永久丧失邻居面。
故 ``acquire`` 的判据是：**pid 等于本进程 pid 的登记必然是本实例的前身**（一个进程
不可能有两个同名实例），接管而非报冲突。Windows reload 走「spawn 新进程 + 旧进程
让位」（PID 变），由 ``acquire(takeover_wait=...)`` 的短暂重试覆盖交替窗口。

防投毒（§1.2）：每实例只写自己的 ``<name>.json``（原子替换）；读取方验 hash +
校验心跳新鲜度。目录是**发现缓存，不是信任源**。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from fp_core.logger import get_logger
from fp_core.platform_utils import FileLock, process_alive
from fp_core.plugins.zeta.domain import (
    Capabilities,
    Endpoint,
    NeighborCard,
    NeighborKind,
    PeerStatus,
    canonical_json,
    sha256_hex,
    verify_card,
)

logger = get_logger()


def default_peers_dir() -> Path:
    """发现目录（运行时求值——便于测试/部署经 FP_ZETA_PEERS_DIR 覆盖）。"""
    return Path(os.environ.get("FP_ZETA_PEERS_DIR") or (Path.home() / ".local" / "share" / "fp" / "peers"))


HEARTBEAT_TTL = 90.0  # offline_threshold = 3× 心跳周期 ≈ 90s


# ─────────────────────────────────────────────────────────
#  发现后端（可插拔）
# ─────────────────────────────────────────────────────────


class DiscoveryBackend(Protocol):
    """发现后端协议：发布自己的卡 / 扫描在线邻居 / 撤回自己的卡。"""

    def publish(self, card: NeighborCard) -> None: ...

    def scan(self, now: float | None = None) -> list[NeighborCard]: ...

    def withdraw(self, name: str) -> None: ...


def _safe_name(name: str) -> str:
    safe = "".join(c for c in name if c.isalnum() or c in "-_.")
    if not safe:
        raise ValueError(f"非法邻居名: {name!r}")
    return safe


class SharedDirBackend:
    """同机共享目录发现后端。

    ``<root>/<name>.json``；写自己的卡用「临时文件 + rename」原子替换，
    绝不触碰别人的文件（§1.2 自写隔离）。
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_peers_dir()

    def card_path(self, name: str) -> Path:
        return self.root / f"{_safe_name(name)}.json"

    def publish(self, card: NeighborCard) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        card.card_hash = card.compute_hash()
        target = self.card_path(card.name)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(canonical_json(card.to_dict()), encoding="utf-8")
        os.replace(tmp, target)

    def scan(self, now: float | None = None) -> list[NeighborCard]:
        if not self.root.exists():
            return []
        t = time.time() if now is None else now
        out: list[NeighborCard] = []
        for f in sorted(self.root.glob("*.json")):
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            try:
                card = NeighborCard.from_dict(raw)
            except (KeyError, ValueError, TypeError):
                continue
            if verify_card(card):  # 篡改 / 半写 / 缺能力 → 丢弃
                continue
            if not card.is_online(t, ttl=HEARTBEAT_TTL):
                continue
            out.append(card)
        return out

    def withdraw(self, name: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.card_path(name).unlink()


# ─────────────────────────────────────────────────────────
#  一目录一实例（§1.5）
# ─────────────────────────────────────────────────────────


class InstanceConflictError(RuntimeError):
    """同目录已有活跃实例。"""


def _pid_alive(pid: Any) -> bool:
    try:
        n = int(pid)
    except (TypeError, ValueError):
        return False
    # 经 platform_utils：os.kill(pid, 0) 的探测语义在 Windows 上不成立
    return process_alive(n)


def _same_process(pid: Any) -> bool:
    """登记的 pid 是否就是**本进程**——reload 的指纹。

    ``pid`` 存活只证明"某个进程还活着"，不证明"还是那个实例"：POSIX reload 用
    ``os.execve`` 替换映像，PID 不变而实例已重生。故 pid == 本进程时这条登记只能是
    本实例的前身（一个进程不可能同时跑两个同名实例），接管而不是报冲突。
    """
    try:
        return int(pid) == os.getpid()
    except (TypeError, ValueError):
        return False


class InstanceRegistry:
    """一 workspace 多实例登记（§1.5 修订版，替换单实例锁）。

    每个实例在 ``<workspace>/.fp/zeta/instances/<name>.json`` 写**自己的**登记
    （自写隔离，§1.2）。同 workspace 可并存多实例 → 天然共享 ``.fp/memory`` 与
    ``.fp/tasks.json``（都是 workspace 级）。

    - 默认（B1 多实例）：仅当**同名**且 pid 存活才冲突（sid 派生名全局唯一，几乎不撞）；
    - **reload 例外**：同名登记的 pid == 本进程 pid ⇒ 是本实例的前身（execve 后 PID
      不变），接管；不再让 reload 把自己认成别人（见模块 docstring）；
    - ``single=True``（保留旧语义）：任何活跃实例存在即冲突，本实例降级不启用邻居面；
    - 每次 acquire 前先 ``sweep_stale``，回收 pid 已死的僵尸登记（防崩溃留痕）。
    """

    def __init__(self, workspace: Path, name: str, *, single: bool = False) -> None:
        self.workspace = Path(workspace)
        self.name = name
        self.single = single
        self.dir = self.workspace / ".fp" / "zeta" / "instances"
        self.path = self.dir / f"{_safe_name(name)}.json"

    def _read_one(self, p: Path) -> dict[str, Any] | None:
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return cast(dict[str, Any], raw) if isinstance(raw, dict) else None

    def scan(self) -> list[dict[str, Any]]:
        """列出同 workspace 所有登记（含僵尸；判活由调用方 ``_pid_alive``）。"""
        if not self.dir.exists():
            return []
        out: list[dict[str, Any]] = []
        for f in sorted(self.dir.glob("*.json")):
            rec = self._read_one(f)
            if rec is not None:
                out.append(rec)
        return out

    def sweep_stale(self) -> int:
        """回收 pid 已死的僵尸登记，返回回收数（损坏文件一并清）。"""
        if not self.dir.exists():
            return 0
        n = 0
        for f in self.dir.glob("*.json"):
            rec = self._read_one(f)
            if rec is None or not _pid_alive(rec.get("pid")):
                with contextlib.suppress(OSError):
                    f.unlink()
                    n += 1
        return n

    def read(self) -> dict[str, Any] | None:
        return self._read_one(self.path)

    def acquire(self, *, sid: str = "", takeover_wait: float = 0.5) -> dict[str, Any]:
        """登记本实例；真正的冲突 → :class:`InstanceConflictError`。

        Args:
            sid: 会话 sid（身份锚点，仅落档便于排查）。
            takeover_wait: 遇到「同名 + pid 存活但**不是**本进程」的登记时，最多等待
                多少秒看它让位。只对 reload 的 spawn 形态（Windows：新进程先起、旧进程
                随后 ``_exit``）有意义——把"交接窗口"与"真冲突"区分开；为 0 则不等待。
        """
        self.sweep_stale()  # 先清僵尸，避免把死实例误判为占用
        if self.single:
            for rec in self.scan():
                if rec.get("name") != self.name and _pid_alive(rec.get("pid")):
                    raise InstanceConflictError(
                        f"目录已有活跃实例 {rec.get('name')!r} (pid={rec.get('pid')})"
                        f"（single 模式拒绝多实例）：{self.workspace}"
                    )
        existing = self.read()
        if existing is not None:
            holder = existing.get("pid")
            if _same_process(holder):
                # reload 的正常形态：POSIX 走 execve，映像换了、PID 没换，旧化身来不及注销。
                # 同一进程不可能有两个同名实例 ⇒ 这条登记必然是我的前身，接管。
                logger.info(f"[zeta] 接管本进程前一化身的登记（reload）: {self.name} (pid={holder})")
            elif _pid_alive(holder):
                deadline = time.monotonic() + max(0.0, takeover_wait)
                while _pid_alive(holder) and time.monotonic() < deadline:
                    time.sleep(0.05)
                if _pid_alive(holder):
                    raise InstanceConflictError(f"实例名 {self.name!r} 已被占用 (pid={holder})：{self.workspace}")
        self.dir.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "name": self.name,
            "sid": sid,
            "pid": os.getpid(),
            "workspace": str(self.workspace),
            "cwd": str(self.workspace),
            "started_at": time.time(),
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    def release(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()


# ─────────────────────────────────────────────────────────
#  名片构造（业务身份 → 路由表）
# ─────────────────────────────────────────────────────────


_HTML_LINE = re.compile(r"^<[^>]*>$")
_LINK_ONLY = re.compile(r"^(?:\[[^\]]*\]\([^)]*\)|!\[[^\]]*\]\([^)]*\))$")
_RULE_LINE = re.compile(r"^[=\-*_~|>`\s]+$")


def _is_readme_noise(text: str) -> bool:
    """README 排版噪声行：HTML 标签、徽章/图片、纯链接、分割线、纯符号。"""
    return (
        (text.startswith("<") and text.endswith(">"))
        or text.startswith("[![")
        or text.startswith("![")
        or bool(_LINK_ONLY.match(text))
        or bool(_RULE_LINE.match(text))
    )


def _strip_md(text: str) -> str:
    """剥离行内 markdown 强调/代码标记，取干净文本。"""
    return text.strip().strip("*_`").strip()


def infer_business(cwd: Path) -> str:
    """从 cwd 推断业务（功能优先：启动声明 > README 描述行 > README 标题 > 目录名）。

    跳过 HTML 标签、徽章、图片、代码围栏等排版噪声行（README 常见），
    取首个有意义的**描述行**；无描述行则退回首个标题，再退回目录名。
    """
    readme = Path(cwd) / "README.md"
    with contextlib.suppress(OSError):
        heading: str | None = None
        in_fence = False
        for raw in readme.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence or not line:
                continue
            if line.startswith("#"):
                body = _strip_md(line.lstrip("#"))
                if body and heading is None:
                    heading = body[:80]
                continue
            if _is_readme_noise(line):
                continue
            return _strip_md(line)[:80]  # 首个干净描述行
        if heading:
            return heading
    return Path(cwd).name


def derive_instance_name(workspace: str | Path, sid: str) -> str:
    """从会话 sid 派生实例名：``<workspace名>-<sid短handle>``（§3.2）。

    - **唯一**：sid 全局唯一 → handle 唯一（同 workspace 多实例天然不撞）；
    - **稳定**：reload/resume 保持同 sid → 名不变（治好现状 pid 派生「reload 换名」）；
    - **不暴露**：handle = sha256(sid) 前 8 位（不可逆），不把本地会话标识泄露给邻居；
    - sid 缺失时退化为随机 handle（防御性；正常 FP 实例必有 sid）。
    """
    ws_name = Path(workspace).name or "fp"
    handle = sha256_hex(sid)[:8] if sid else os.urandom(4).hex()
    return f"{ws_name}-{handle}"


def build_card(
    *,
    name: str,
    cwd: str | Path,
    workspace: str | Path | None = None,
    business: str = "",
    tags: list[str] | None = None,
    endpoints: list[Endpoint] | None = None,
    codec: str = "a2a",
    profiles: list[str] | None = None,
    roles: list[str] | None = None,
    status: PeerStatus = PeerStatus.IDLE,
    current_task: str | None = None,
    kind: NeighborKind = NeighborKind.FP_INSTANCE,
) -> NeighborCard:
    """构造一张自洽的名片（hash 已算好，可直接 publish）。

    ``workspace`` 是项目归属（路由键）；缺省回退 ``cwd``（C(a)：workspace = 启动目录）。
    """
    now = time.time()
    card = NeighborCard(
        name=name,
        kind=kind,
        workspace=str(workspace or cwd),
        cwd=str(cwd),
        business=business or infer_business(Path(cwd)),
        tags=list(tags or []),
        status=status,
        current_task=current_task,
        started_at=now,
        endpoints=list(endpoints or []),
        codec=codec,
        capabilities=Capabilities(
            profiles=list(profiles or ["fp-contract-profile", "bare-a2a"]),
            semantic_layers="SEMANTIC_CAPABLE",
            supports_idempotency=True,
            skills=list(tags or []),
        ),
        roles=list(roles or []),
        heartbeat=now,
        issued_at=now,
    )
    card.card_hash = card.compute_hash()
    return card


def refresh_heartbeat(card: NeighborCard, *, status: PeerStatus | None = None) -> None:
    """刷新心跳（可选更新 status / current_task），并重算 hash。"""
    card.heartbeat = time.time()
    if status is not None:
        card.status = status
    card.card_hash = card.compute_hash()


# ─────────────────────────────────────────────────────────
#  会话 → workspace 记录（E(ii)：resume 跨目录的错配提示）
# ─────────────────────────────────────────────────────────


def zeta_state_dir() -> Path:
    """Zeta 全局状态目录（不是 workspace 级）：``~/.local/share/fp/zeta/``。"""
    return Path(os.environ.get("FP_ZETA_STATE_DIR") or (Path.home() / ".local" / "share" / "fp" / "zeta"))


def check_session_workspace(sid: str, workspace: str | Path) -> str | None:
    """记录 ``sid → workspace``，返回该 sid **上次**所在的 workspace（若与当前不同）。

    E(ii)：sid 全局、``.fp/memory`` 是 workspace 级 —— 两者正交。在别的目录
    ``fp -r <sid>`` 时，人格（会话）来自旧目录、记忆读新目录 → 错配。
    返回非 None 即表示错配，调用方提示一次。
    """
    if not sid:
        return None
    f = zeta_state_dir() / "session_workspaces.json"
    data: dict[str, str] = {}
    with contextlib.suppress(OSError, json.JSONDecodeError):
        loaded = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = {str(k): str(v) for k, v in cast(dict[str, Any], loaded).items()}
    prev = data.get(sid)
    data[sid] = str(workspace)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, f)
    return prev if prev and prev != str(workspace) else None


# ─────────────────────────────────────────────────────────
#  共享目录卫生：可达性 GC（方案 B）
# ─────────────────────────────────────────────────────────

GC_CARD_GRACE = 7 * 86400.0  # 心跳过期多久后才「可能」回收（TTL 只判离线，回收要更保守）
GC_LOCK_NAME = ".gc.lock"


@dataclass
class GcReport:
    """一次 GC 的结果（``dry_run`` 时是「将会」的数字）。"""

    cards: int = 0
    inboxes: int = 0
    kept_with_mail: int = 0
    bytes_freed: int = 0
    skipped_by_lock: bool = False

    def __bool__(self) -> bool:
        """是否真的动了东西（按此决定要不要打日志）。"""
        return bool(self.cards or self.inboxes)

    def summary(self) -> str:
        if self.skipped_by_lock:
            return "共享目录 GC：另一实例正在跑，本次跳过"
        text = (
            f"共享目录 GC：回收名片 {self.cards} 张 / 收件箱 {self.inboxes} 个 / 释放 {self.bytes_freed / 1024:.1f} KB"
        )
        if self.kept_with_mail:
            text += f"；保留 {self.kept_with_mail} 个仍有未读留话的收件箱"
        return text


def instance_alive(workspace: str | Path, name: str) -> bool | None:
    """``workspace`` 下是否还有 ``name`` 的**活登记**（回收名片的第二道判据）。

    True=有活实例；False=**确定**没有（workspace/登记目录/登记文件不在，或 pid 已死）；
    None=无法判定（读失败）——调用方须保守当"还在"处理，宁留勿删。
    """
    if not str(workspace or "").strip():
        return None
    ws = Path(str(workspace))
    try:
        if not ws.is_dir():
            return False  # workspace 都没了 → 该实例不可能活着
        d = ws / ".fp" / "zeta" / "instances"
        if not d.is_dir():
            return False  # 活实例 acquire 必建登记目录
        p = d / f"{_safe_name(name)}.json"
        if not p.exists():
            return False  # 登记随正常退出删除 → 没有登记即已退出
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    return _pid_alive(cast(dict[str, Any], rec).get("pid"))


def _file_size(p: Path) -> int:
    with contextlib.suppress(OSError):
        return p.stat().st_size
    return 0


def _tree_size(d: Path) -> int:
    total = 0
    with contextlib.suppress(OSError):
        for f in d.rglob("*"):
            if f.is_file():
                total += _file_size(f)
    return total


def collect_garbage(
    backend: SharedDirBackend | None = None,
    *,
    card_grace: float = GC_CARD_GRACE,
    now: float | None = None,
    dry_run: bool = False,
) -> GcReport:
    """回收共享发现目录里的**死残留**——判据是「可达性」，不是「时间到了」。

    名片要删需**同时**满足两条（缺一不删，宁留勿删）：
      1. 心跳过期超过 ``card_grace``（默认 7 天；TTL 90s 只判"离线"，与"可回收"是两回事）;
      2. 其 ``workspace`` 下已无同名**活登记**（回查 instances 的 pid；判不了就留）。
    名片本质是派生缓存（活实例每 2s 重发），误删也会秒级自愈；真正的红线上在收件箱。

    收件箱：**顶层有未读留话 → 永不删**——那是"给未来自己的话"，名字由 sid 派生，
    会话 resume 即同名、上线即读到；无未读且对应名片已死/不存在 → 整目录回收
    （含 ``.processed``：它只由**活**实例的 housekeeping 清理，实例死了就永无人清）。

    多实例并发：经 ``FileLock`` 保证同一时刻只有一个实例在做（拿不到锁就跳过，不算失败）。
    """
    be = backend or SharedDirBackend()
    root = be.root
    t = time.time() if now is None else now
    rep = GcReport()
    if not root.is_dir():
        return rep

    lock = FileLock(str(root / GC_LOCK_NAME))
    if not dry_run and not lock.acquire():
        rep.skipped_by_lock = True
        return rep
    try:
        keep: set[str] = set()
        doomed_cards: list[Path] = []
        for f in sorted(root.glob("*.json")):
            raw: dict[str, Any] | None = None
            with contextlib.suppress(OSError, json.JSONDecodeError):
                loaded = json.loads(f.read_text(encoding="utf-8"))
                raw = cast(dict[str, Any], loaded) if isinstance(loaded, dict) else None
            if raw is not None:
                hb = float(raw.get("heartbeat") or 0)
                if t - hb <= card_grace:
                    keep.add(f.stem)  # 心跳仍新鲜：活着
                    continue
                # 回查该实例的登记：True/None（保守）→ 留；False → 确定不在了
                if instance_alive(str(raw.get("workspace") or ""), str(raw.get("name") or f.stem)) is not False:
                    keep.add(f.stem)
                    continue
            else:
                # 损坏/半写：无可回查的 workspace —— 按文件龄保守处理
                if t - _mtime(f) <= card_grace:
                    keep.add(f.stem)
                    continue
            doomed_cards.append(f)

        doomed_inboxes: list[Path] = []
        for d in sorted(root.glob("*.inbox")):
            if not d.is_dir():
                continue
            if any(d.glob("msg-*.json")):
                rep.kept_with_mail += 1  # 有未读留话：永不删
                continue
            if d.name[: -len(".inbox")] in keep:
                continue  # 活实例自己的收件箱，由它的 housekeeping 管
            doomed_inboxes.append(d)

        for f in doomed_cards:
            rep.cards += 1
            rep.bytes_freed += _file_size(f)
            if not dry_run:
                with contextlib.suppress(OSError):
                    f.unlink()
        for d in doomed_inboxes:
            rep.inboxes += 1
            rep.bytes_freed += _tree_size(d)
            if not dry_run:
                shutil.rmtree(d, ignore_errors=True)

        if not dry_run and rep:
            logger.info(f"[zeta] {rep.summary()}")
        return rep
    finally:
        lock.release()


def _mtime(p: Path) -> float:
    with contextlib.suppress(OSError):
        return p.stat().st_mtime
    return 0.0
