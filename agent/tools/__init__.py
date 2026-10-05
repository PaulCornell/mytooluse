from agent.tools.base import Tool, ToolContext, ToolOutcome, ToolRegistry
from agent.tools.python_exec import RunPythonTool
from agent.tools.search import WebSearchTool
from agent.tools.sql import GetSchemaTool, RunSqlTool


def default_tools(*, include_search: bool | None = None) -> list[Tool]:
    """The standard tool set. web_search is included only when TAVILY_API_KEY is set (TOOL-7)."""
    if include_search is None:
        include_search = WebSearchTool.available()
    tools: list[Tool] = [GetSchemaTool(), RunSqlTool(), RunPythonTool()]
    if include_search:
        tools.append(WebSearchTool())
    return tools


__all__ = [
    "Tool", "ToolContext", "ToolOutcome", "ToolRegistry",
    "GetSchemaTool", "RunSqlTool", "RunPythonTool", "WebSearchTool", "default_tools",
]
