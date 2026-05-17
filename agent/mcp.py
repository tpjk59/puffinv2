"""MacroFactor MCP client.

Wraps the remote MCP HTTP server as Anthropic-compatible tool definitions.
When MACROFACTOR_MCP_URL is not set, all functions are no-ops and
mf_configured() returns False — the agent skips nutrition entirely.
"""

import json
import logging
import os
from typing import Any

import httpx

_log = logging.getLogger(__name__)

MF_MCP_URL: str = os.getenv("MACROFACTOR_MCP_URL", "")
MF_MCP_TOKEN: str = os.getenv("MACROFACTOR_MCP_TOKEN", "")
MF_TOOL_PREFIX = "mf_"

_tools_cache: list[dict] | None = None


def mf_configured() -> bool:
    return bool(MF_MCP_URL)


def _make_http_client() -> httpx.AsyncClient:
    headers: dict[str, str] = {}
    if MF_MCP_TOKEN:
        headers["Authorization"] = f"Bearer {MF_MCP_TOKEN}"
    return httpx.AsyncClient(headers=headers, timeout=30.0)


async def get_mf_tool_definitions() -> list[dict]:
    """Return Anthropic-format tool definitions from the MCP server.

    Results are cached after the first successful call. Falls back to [] if
    the server is unreachable rather than crashing startup.
    """
    global _tools_cache
    if _tools_cache is not None:
        return _tools_cache

    if not MF_MCP_URL:
        _tools_cache = []
        return []

    try:
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(MF_MCP_URL, http_client=_make_http_client()) as (read, write):
            async with ClientSession(read, write) as sess:
                await sess.initialize()
                result = await sess.list_tools()
                _tools_cache = [_to_anthropic_tool(t) for t in result.tools]
                _log.info("Loaded %d MacroFactor MCP tools", len(_tools_cache))
                return _tools_cache
    except Exception as exc:
        _log.warning("MacroFactor MCP tool list unavailable: %s", exc)
        _tools_cache = []
        return []


async def call_mf_tool(name: str, args: dict[str, Any]) -> dict:
    """Call a MacroFactor MCP tool by its unprefixed name and return parsed JSON."""
    if not MF_MCP_URL:
        return {"error": "MacroFactor MCP not configured"}

    try:
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(MF_MCP_URL, http_client=_make_http_client()) as (read, write):
            async with ClientSession(read, write) as sess:
                await sess.initialize()
                result = await sess.call_tool(name, args)
                if result.isError:
                    error_text = " ".join(
                        c.text for c in result.content if hasattr(c, "text")
                    )
                    return {"error": error_text or "MCP tool returned an error"}
                for block in result.content:
                    if hasattr(block, "text"):
                        try:
                            return json.loads(block.text)
                        except json.JSONDecodeError:
                            return {"result": block.text}
                return {"error": "No content returned from MCP tool"}
    except Exception as exc:
        _log.error("MacroFactor MCP call failed for %s: %s", name, exc)
        return {"error": f"MacroFactor MCP call failed: {exc}"}


def _to_anthropic_tool(tool: Any) -> dict:
    """Convert an MCP Tool object to Anthropic API tool definition format."""
    schema = tool.inputSchema
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump(exclude_none=True)
    elif not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}

    return {
        "name": f"{MF_TOOL_PREFIX}{tool.name}",
        "description": tool.description or "",
        "input_schema": schema,
    }
