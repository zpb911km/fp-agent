"""
Web Fetch 插件 — 抓取网页内容（异步版本）

使用 httpx 库抓取网页并返回纯文本内容。
"""

import re
from typing import Any

import httpx

# ── 插件定义（OpenAI function calling schema） ──────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": "抓取并解析网页内容，返回纯文本。超时 15 秒。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "网页完整 URL"},
            },
            "required": ["url"],
        },
    },
}


async def execute(params: dict[str, Any]) -> str:
    """
    抓取网页内容（异步）

    Args:
        params: 包含 'url' 键的字典

    Returns:
        网页纯文本内容（前 5000 字符）
    """
    url = params.get("url", "")
    if not url:
        raise ValueError("web_fetch 插件需要 url 参数")

    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
        ) as client:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; AIAgent/1.0)",
                },
            )
            resp.raise_for_status()
            html = resp.text

        # 剥离 script/style 标签
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        # 去除 HTML 标签
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        if len(text) > 5000:
            text = text[:5000] + f"\n...（已截断，原文 {len(text)} 字符）"

        return text

    except Exception as e:
        return f"抓取错误：{e}"
