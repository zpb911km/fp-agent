"""
Web Search 插件 - 网络搜索

使用 ddgs 库进行网络搜索（原 duckduckgo_search）。
需要安装：pip install ddgs
"""

from typing import Dict, Any


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


def execute(params: Dict[str, Any]) -> str:
    """
    执行网络搜索
    
    Args:
        params: 包含 'query' 键的字典
        
    Returns:
        搜索结果字符串
    """
    query = params.get("query")
    
    if not query:
        raise ValueError("web_search 插件需要 query 参数")
    
    try:
        from ddgs import DDGS
        
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
        
        output_lines = [f"🔍 搜索 '{query}' 的结果:\n"]
        
        for i, result in enumerate(results, 1):
            output_lines.append(f"\n{i}. {result['title']}")
            output_lines.append(f"   URL: {result['href']}")
            snippet = result.get('body', '')[:200]
            output_lines.append(f"   摘要：{snippet}...")
        
        return "\n".join(output_lines)
        
    except ImportError:
        return "❌ 错误：未安装 ddgs 库\n请运行：pip install ddgs"
    except Exception as e:
        return f"❌ 搜索失败：{e}"
