"""
Web Search 插件 — 网络搜索（异步版本）

使用 ddgs 库进行网络搜索。
需要安装：pip install ddgs
"""

import asyncio
from typing import Any, Dict


# ── 插件定义（OpenAI function calling schema） ──────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "搜索互联网获取最新信息。需要安装 ddgs 库。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
            },
            "required": ["query"],
        },
    },
}


async def execute(params: Dict[str, Any]) -> str:
    """
    执行网络搜索（异步）
    
    Args:
        params: 包含 'query' 键的字典
        
    Returns:
        搜索结果字符串
    """
    query = params.get("query", "")
    if not query:
        raise ValueError("web_search 插件需要 query 参数")
    
    try:
        from ddgs import DDGS
        
        loop = asyncio.get_running_loop()
        
        def _search():
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=5))
        
        results = await loop.run_in_executor(None, _search)
        
        if not results:
            return "未找到相关结果"
        
        output_lines = [f"🔍 搜索 '{query}' 的结果:\n"]
        for i, r in enumerate(results, 1):
            output_lines.append(f"\n{i}. {r.get('title', '无标题')}")
            output_lines.append(f"   URL: {r.get('href', '')}")
            snippet = r.get('body', '')[:200]
            if snippet:
                output_lines.append(f"   摘要：{snippet}...")
        
        return "\n".join(output_lines)
    
    except ImportError:
        return "错误：未安装 ddgs 库（pip install ddgs）"
    except Exception as e:
        return f"错误：{e}"
