"""ext scanner — 静态代码扫描（AST 模式匹配，不执行代码）

一张规则表 + 场景参数：
  install（安装审查）→ 全量规则，关注风险行为（命令执行/网络/文件写入/反序列化）
  promote（分享审查）→ 追加隐私规则（硬编码绝对路径/密钥/人名邮箱）

每个命中输出 {rule_id, line, severity, evidence}。
"""

import ast
import os
import re
from dataclasses import dataclass

# ── 规则表 ───────────────────────────────────────────────────────
# severity: high / medium / low
# scenes: 适用场景（install=安装审查, promote=分享审查）
# 命中模式用 AST 节点匹配函数描述


@dataclass
class Hit:
    rule_id: str
    line: int
    severity: str
    evidence: str
    scene: str


# 高危：任意代码/命令执行
EXEC_NAMES = {
    "eval",
    "exec",
    "compile",
    "os.system",
    "os.popen",
    "subprocess.run",
    "subprocess.call",
    "subprocess.Popen",
    "subprocess.check_call",
    "subprocess.check_output",
    "pty.spawn",
    "commands.getoutput",
}
# 高危：反序列化/解码链
DESERIALIZE_NAMES = {"pickle.loads", "pickle.load", "marshal.loads", "marshal.load", "shelve.open", "base64.b64decode"}
# 中危：网络外发
NETWORK_NAMES = {
    "requests.get",
    "requests.post",
    "requests.put",
    "requests.delete",
    "requests.request",
    "urllib.request.urlopen",
    "socket.socket",
    "http.client.HTTPConnection",
    "http.client.HTTPSConnection",
}
# 中危：文件读取敏感路径
SENSITIVE_PATTERNS = [
    re.compile(r"(?i)\.ssh[/\\]|id_rsa|id_ed25519|\.aws[/\\]|credentials|api[_-]?key|token|secret"),
]
# 隐私（promote）：硬编码绝对路径 / 密钥 / 人名邮箱
PRIVACY_PATTERNS = [
    re.compile(r"(?i)(?:/home/|/Users/|C:\\\\Users\\\\|/media/|/mnt/)[^\"']+"),
    re.compile(r"(?i)\b(sk-[a-zA-Z0-9]{16,}|api[_-]?key\s*[:=]\s*['\"][^'\"]{8,}|token\s*[:=]\s*['\"][^'\"]{8,})"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
]
# 用户本人标识（可配置，来自环境或传入）
USERNAME_HINT = os.environ.get("USER") or os.environ.get("USERNAME") or ""


def _module_fullname(node: ast.AST) -> str | None:
    """从 AST 节点提取可调用全名，如 os.system / subprocess.run / requests.get。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _module_fullname(node.value)
        if base:
            return f"{base}.{node.attr}"
        return node.attr
    return None


def _scan_call(node: ast.Call, hits: list[Hit], scene: str):
    """检查一个 Call 节点。"""
    full = _module_fullname(node.func)
    if not full:
        return
    # 命令执行 / 反序列化
    if full in EXEC_NAMES or full.split(".")[-1] in ("eval", "exec"):
        severity = "high"
        hits.append(Hit("code-exec", node.lineno, severity, full, scene))
    elif full in DESERIALIZE_NAMES:
        hits.append(Hit("deserialize", node.lineno, "high", full, scene))
    elif full in NETWORK_NAMES:
        hits.append(Hit("network", node.lineno, "medium", full, scene))
    # open 写模式（非临时路径）
    elif full == "open":
        mode = ""
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            mode = str(node.args[1].value)
        elif node.keywords:
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = str(kw.value.value)
        if "w" in mode or "a" in mode or "+" in mode:
            hits.append(Hit("file-write", node.lineno, "medium", f"open(mode={mode!r})", scene))


def _scan_import(node: ast.AST, hits: list[Hit], scene: str):
    """检查 Import / ImportFrom 节点。"""
    if isinstance(node, ast.Import):
        names = [a.name for a in node.names]
    elif isinstance(node, ast.ImportFrom):
        names = [node.module or ""] + [a.name for a in node.names]
    else:
        return
    for name in names:
        base = name.split(".")[0]
        if base in ("pickle", "marshal", "shelve", "base64"):
            hits.append(Hit("deserialize-import", node.lineno, "high", f"import {base}", scene))
        elif base in ("socket", "requests", "urllib", "http", "ftplib", "smtplib", "telnetlib"):
            hits.append(Hit("network-import", node.lineno, "medium", f"import {base}", scene))
        elif base in ("subprocess", "os", "sys", "pty"):
            hits.append(Hit("exec-import", node.lineno, "medium", f"import {base}", scene))


def _scan_string_privacies(node: ast.AST, hits: list[Hit]):
    """分享场景：扫描字符串常量中的隐私内容。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        s = node.value
        for pat in PRIVACY_PATTERNS:
            m = pat.search(s)
            if m:
                snippet = m.group(0)[:60]
                hits.append(Hit("privacy", node.lineno, "high", snippet, "promote"))
                break
        # 用户名提示（可选）
        if USERNAME_HINT and USERNAME_HINT.lower() in s.lower():
            hits.append(Hit("privacy-user", node.lineno, "medium", USERNAME_HINT, "promote"))


def scan_file(filepath: str, scene: str = "install") -> list[Hit]:
    """扫描单个文件，返回风险点列表（不执行代码）。

    Args:
        filepath: 目标文件
        scene: "install"（全量规则）或 "promote"（含隐私规则）
    """
    hits: list[Hit] = []
    if not filepath.endswith(".py"):
        return hits
    try:
        with open(filepath, encoding="utf-8") as f:
            src = f.read()
    except OSError:
        return hits
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return hits

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            _scan_call(node, hits, scene)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            _scan_import(node, hits, scene)
        if scene == "promote":
            _scan_string_privacies(node, hits)

    return hits


def scan_directory(directory: str, scene: str = "install") -> dict[str, list[Hit]]:
    """扫描目录下所有 .py 文件（递归），返回 {文件相对路径: [Hit, ...]}。"""
    results: dict[str, list[Hit]] = {}
    for root, _dirs, files in os.walk(directory):
        for fname in sorted(files):
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, directory)
            hits = scan_file(fpath, scene)
            if hits:
                results[rel] = hits
    return results


def format_report(hits: dict[str, list[Hit]]) -> str:
    """将扫描结果格式化为可读报告。"""
    hits = {k: v for k, v in hits.items() if v}  # 过滤空命中
    if not hits:
        return "✅ 未发现风险点"
    lines = ["⚠️  发现风险点（以下为静态特征，需人工/AI 确认）：", ""]
    for fname, hs in hits.items():
        lines.append(f"📄 {fname}")
        for h in hs:
            tag = {"high": "🔴", "medium": "🟠", "low": "🟡"}.get(h.severity, "🟡")
            lines.append(f"  {tag} [{h.severity}] L{h.line} {h.rule_id}: {h.evidence}")
        lines.append("")
    return "\n".join(lines)
