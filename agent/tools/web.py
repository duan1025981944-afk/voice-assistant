"""用默认浏览器搜索或打开网址。"""
from __future__ import annotations

import webbrowser
from urllib.parse import quote_plus, urlparse

from agent.tools.base import Tool, ToolResult
from config.loader import load_voice_settings

ENGINES = {
    "bing": "https://cn.bing.com/search?q={}",
    "baidu": "https://www.baidu.com/s?wd={}",
}


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "在浏览器里打开搜索结果页，如'搜一下明天北京天气'。"
        "只负责打开页面给用户看，你自己看不到结果。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索词，如 明天北京天气"},
            "engine": {"type": "string", "enum": list(ENGINES), "description": "不填用配置里的默认引擎"},
        },
        "required": ["query"],
    }
    needs_confirm = False

    async def execute(self, query: str, engine: str | None = None) -> str:
        engine = engine or load_voice_settings().search_engine
        template = ENGINES.get(engine, ENGINES["bing"])
        webbrowser.open(template.format(quote_plus(query)))
        return f"已在浏览器里搜索：{query}"


class OpenUrlTool(Tool):
    name = "open_url"
    description = "用默认浏览器打开一个网址，如 bilibili.com。只支持 http/https 网址，不能打开本地文件。"
    parameters = {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "网址，如 https://www.bilibili.com"}},
        "required": ["url"],
    }
    needs_confirm = False

    async def execute(self, url: str) -> str:
        if "://" not in url:
            url = "https://" + url
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return ToolResult.error(f"只能打开 http/https 网址：{url}")
        webbrowser.open(url)
        return f"已打开 {parsed.netloc}"
