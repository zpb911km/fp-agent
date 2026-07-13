"""
Web Search 插件 — 网络搜索（异步版本）

使用 httpx 直接请求搜索引擎的 HTML 页面并解析结果。
支持后端：Bing（默认）、DuckDuckGo（备选 fallback）。
不再依赖 ddgs 库。
"""

import asyncio
from html.parser import HTMLParser
from typing import Any

import httpx

# ── 插件定义 ───────────────────────────────────────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "快速搜索 - 直接爬取搜索引擎（Bing/DuckDuckGo）返回标题+链接+摘要列表。"
        "适合查简单事实。速度快，不做深度阅读。如果需分析多篇文章请用 smart_web_search。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "backend": {
                    "type": "string",
                    "enum": ["bing", "duckduckgo"],
                    "description": "搜索引擎（默认 bing）",
                },
            },
            "required": ["query"],
        },
    },
}


# ── HTML 解析器 ────────────────────────────────────────────────────


class BingResultParser(HTMLParser):
    """解析 Bing <li class='b_algo'> 结构"""

    def __init__(self):
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._cur: dict[str, str] = {}
        self._in_algo = False
        self._in_h2 = False  # 标题只在 <h2> 内捕获
        self._in_title_link = False
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag in ("script", "style"):
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return

        classes = d.get("class") or ""
        if tag == "li" and "b_algo" in classes.split():
            self._in_algo = True
            self._cur = {}
            return

        if not self._in_algo:
            return

        if tag == "h2":
            self._in_h2 = True

        if tag == "a" and self._in_h2:
            href = d.get("href") or ""
            if href and not href.startswith("#"):
                self._cur["href"] = href
                self._in_title_link = True

    def handle_endtag(self, tag):
        if self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if tag == "li" and self._in_algo:
            self._in_algo = False
            if self._cur.get("title") and self._cur.get("href"):
                self.results.append(self._cur)
            self._cur = {}
        if tag == "h2" and self._in_h2:
            self._in_h2 = False
        if tag == "a" and self._in_title_link:
            self._in_title_link = False

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        if self._in_title_link and self._in_h2:
            self._cur["title"] = self._cur.get("title", "") + data


class DuckDuckGoResultParser(HTMLParser):
    """解析 DuckDuckGo HTML 结果 <div class='result'> 结构"""

    def __init__(self):
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._cur: dict[str, str] = {}
        self._in_result = False
        self._in_title = False
        self._in_snippet = False
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag in ("script", "style"):
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return

        classes = d.get("class") or ""
        if tag == "div" and classes and "result" in classes.split():
            self._in_result = True
            self._cur = {}
            return

        if not self._in_result:
            return

        if tag == "a" and "result__a" in classes.split():
            self._in_title = True
            self._cur["href"] = d.get("href") or ""

        if tag == "a" and "result__snippet" in classes.split():
            self._in_snippet = True

    def handle_endtag(self, tag):
        if self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if tag == "div" and self._in_result:
            self._in_result = False
            if self._cur.get("title") and self._cur.get("href"):
                self.results.append(self._cur)
            self._cur = {}
        if tag == "a" and self._in_title:
            self._in_title = False
        if tag == "a" and self._in_snippet:
            self._in_snippet = False

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        if self._in_title:
            self._cur["title"] = self._cur.get("title", "") + data
        if self._in_snippet:
            self._cur["body"] = self._cur.get("body", "") + data


# ── 搜索后端（同步函数，在 run_in_executor 中执行） ────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _search_bing_sync(query: str) -> list[dict[str, str]]:
    """Bing 搜索（同步，在 executor 中运行）"""
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        resp = client.get(
            "https://www.bing.com/search",
            params={"q": query, "count": "5"},
            headers=HEADERS,
        )
        resp.raise_for_status()
        parser = BingResultParser()
        parser.feed(resp.text)
        return parser.results


def _search_duckduckgo_sync(query: str) -> list[dict[str, str]]:
    """DuckDuckGo 搜索（同步，在 executor 中运行）"""
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        resp = client.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers=HEADERS,
        )
        resp.raise_for_status()
        parser = DuckDuckGoResultParser()
        parser.feed(resp.text)
        return parser.results


# ── 结果格式化 ─────────────────────────────────────────────────────


def _format_results(query: str, results: list[dict[str, str]], backend: str) -> str:
    if not results:
        return "未找到相关结果"

    label = {"bing": "Bing", "duckduckgo": "DuckDuckGo"}
    icon = {"bing": "🔍", "duckduckgo": "🦆"}
    lines = [f"{icon.get(backend, '🔍')} 搜索 '{query}' 的结果 ({label.get(backend, backend)}):\n"]
    for i, r in enumerate(results[:5], 1):
        title = r.get("title", "无标题").strip()
        href = r.get("href", "")
        snippet = (r.get("body", "") or "")[:200]
        lines.append(f"\n{i}. {title}")
        lines.append(f"   URL: {href}")
        if snippet:
            lines.append(f"   摘要：{snippet}...")
    return "\n".join(lines)


# ── 主入口（异步） ─────────────────────────────────────────────────

BACKENDS = {
    "bing": _search_bing_sync,
    "duckduckgo": _search_duckduckgo_sync,
}

FALLBACK_ORDER = ["bing", "duckduckgo"]


async def execute(params: dict[str, Any]) -> str:
    query = params.get("query", "")
    preferred = params.get("backend", "").lower().strip()

    if not query:
        raise ValueError("web_search 插件需要 query 参数")

    # 确定后端尝试顺序
    order = [preferred] + [b for b in FALLBACK_ORDER if b != preferred] if preferred in BACKENDS else FALLBACK_ORDER

    loop = asyncio.get_running_loop()
    last_error = None

    for backend in order:
        try:
            results = await loop.run_in_executor(None, BACKENDS[backend], query)
            if results:
                return _format_results(query, results, backend)
        except Exception as e:
            last_error = e
            continue

    if last_error:
        return f"错误：所有搜索后端均失败。最后错误：{last_error}"
    return "未找到相关结果"
