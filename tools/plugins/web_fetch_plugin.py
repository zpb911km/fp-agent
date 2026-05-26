"""
Web Fetch 插件 - 抓取网页内容

使用 requests 库抓取网页并提取纯文本内容。
"""

import re
import tempfile
import os
from typing import Dict, Any


# ── 插件定义（OpenAI function calling schema） ──────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": "抓取并解析网页内容，返回纯文本。超时 15 秒。需要安装 requests 库。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "网页完整 URL"},
            },
            "required": ["url"],
        },
    },
}


def execute(params: dict) -> str:
    """
    抓取网页内容
    
    Args:
        params: 包含 'url' 键的字典
        
    Returns:
        网页纯文本内容
    """
    url = params.get("url")
    
    if not url:
        raise ValueError("web_fetch 插件需要 url 参数")
    
    try:
        import requests
        
        resp = requests.get(
            url, 
            timeout=15, 
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; AIAgent/1.0)",
            }
        )
        resp.raise_for_status()
        html = resp.text

        # 剥离 script/style 标签
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        
        return text[:5000] if len(text) > 5000 else text
        
    except ImportError:
        return "需要安装 requests: pip install requests"
    except Exception as e:
        return f"抓取错误：{e}"
