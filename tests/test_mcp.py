"""Tests for agent/mcp.py — all MCP HTTP calls mocked."""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agent.mcp as mcp_module
from agent.mcp import _to_anthropic_tool, call_mf_tool, get_mf_tool_definitions, mf_configured


@pytest.fixture(autouse=True)
def reset_cache():
    """Clear the module-level tool cache before and after each test."""
    mcp_module._tools_cache = None
    yield
    mcp_module._tools_cache = None


@pytest.fixture(autouse=True)
def no_mf_url(monkeypatch):
    """Default to no MF URL so tests are opt-in."""
    monkeypatch.setattr(mcp_module, "MF_MCP_URL", "")
    monkeypatch.setattr(mcp_module, "MF_MCP_TOKEN", "")


def _make_tool(name: str, description: str = "", schema: dict | None = None) -> MagicMock:
    t = MagicMock()
    t.name = name
    t.description = description
    t.inputSchema = schema or {"type": "object", "properties": {}}
    return t


def _mock_mcp(tools=None, call_text: str | None = None, call_is_error: bool = False):
    """Return (http_ctx_patch, session_cls_patch) mocks for the two MCP context managers.

    http_ctx_patch: patches streamable_http_client to yield (read, write).
    session_cls_patch: patches ClientSession so that ClientSession(r, w) yields a
    session with list_tools / call_tool pre-configured.
    """
    session = AsyncMock()
    session.initialize = AsyncMock()

    if tools is not None:
        list_result = MagicMock()
        list_result.tools = tools
        session.list_tools = AsyncMock(return_value=list_result)

    if call_text is not None:
        call_result = MagicMock()
        call_result.isError = call_is_error
        block = MagicMock()
        block.text = call_text
        call_result.content = [block]
        session.call_tool = AsyncMock(return_value=call_result)

    @asynccontextmanager
    async def _http_cm(*args, **kwargs):
        yield MagicMock(), MagicMock()

    class _SessionCM:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            pass

    return _http_cm, _SessionCM


# ---------------------------------------------------------------------------
# mf_configured
# ---------------------------------------------------------------------------


def test_mf_configured_false_when_no_url():
    assert mf_configured() is False


def test_mf_configured_true_when_url_set(monkeypatch):
    monkeypatch.setattr(mcp_module, "MF_MCP_URL", "https://mcp.example.com")
    assert mf_configured() is True


# ---------------------------------------------------------------------------
# get_mf_tool_definitions
# ---------------------------------------------------------------------------


async def test_get_mf_tool_definitions_returns_empty_when_not_configured():
    result = await get_mf_tool_definitions()
    assert result == []
    assert mcp_module._tools_cache == []


async def test_get_mf_tool_definitions_fetches_and_converts_tools(monkeypatch):
    monkeypatch.setattr(mcp_module, "MF_MCP_URL", "https://mcp.example.com")
    tools = [
        _make_tool("log_food", "Log a food item"),
        _make_tool("get_context", "Get today's context"),
    ]
    http_cm, session_cls = _mock_mcp(tools=tools)

    with patch("mcp.client.streamable_http.streamable_http_client", http_cm), \
         patch("mcp.client.session.ClientSession", session_cls):
        result = await get_mf_tool_definitions()

    assert len(result) == 2
    names = {t["name"] for t in result}
    assert names == {"mf_log_food", "mf_get_context"}
    # Confirm Anthropic tool shape
    for t in result:
        assert "description" in t
        assert "input_schema" in t


async def test_get_mf_tool_definitions_caches_result(monkeypatch):
    monkeypatch.setattr(mcp_module, "MF_MCP_URL", "https://mcp.example.com")
    tools = [_make_tool("log_food")]
    http_cm, session_cls = _mock_mcp(tools=tools)

    with patch("mcp.client.streamable_http.streamable_http_client", http_cm), \
         patch("mcp.client.session.ClientSession", session_cls):
        first = await get_mf_tool_definitions()
        # Second call — server mocks would raise if called again, but caching avoids it
        second = await get_mf_tool_definitions()

    assert first is second


async def test_get_mf_tool_definitions_returns_empty_on_server_error(monkeypatch):
    monkeypatch.setattr(mcp_module, "MF_MCP_URL", "https://mcp.example.com")

    async def _boom(*args, **kwargs):
        raise ConnectionError("server down")
        yield  # make it an async generator so asynccontextmanager is happy

    with patch("mcp.client.streamable_http.streamable_http_client", side_effect=Exception("down")):
        result = await get_mf_tool_definitions()

    assert result == []


# ---------------------------------------------------------------------------
# call_mf_tool
# ---------------------------------------------------------------------------


async def test_call_mf_tool_returns_error_when_not_configured():
    result = await call_mf_tool("log_food", {"food": "chicken"})
    assert "error" in result


async def test_call_mf_tool_returns_parsed_json(monkeypatch):
    monkeypatch.setattr(mcp_module, "MF_MCP_URL", "https://mcp.example.com")
    payload = {"logged": True, "calories": 250}
    http_cm, session_cls = _mock_mcp(call_text=json.dumps(payload))

    with patch("mcp.client.streamable_http.streamable_http_client", http_cm), \
         patch("mcp.client.session.ClientSession", session_cls):
        result = await call_mf_tool("log_food", {"food": "chicken breast"})

    assert result == payload


async def test_call_mf_tool_returns_text_when_not_json(monkeypatch):
    monkeypatch.setattr(mcp_module, "MF_MCP_URL", "https://mcp.example.com")
    http_cm, session_cls = _mock_mcp(call_text="OK")

    with patch("mcp.client.streamable_http.streamable_http_client", http_cm), \
         patch("mcp.client.session.ClientSession", session_cls):
        result = await call_mf_tool("ping", {})

    assert result == {"result": "OK"}


async def test_call_mf_tool_returns_error_on_mcp_error_response(monkeypatch):
    monkeypatch.setattr(mcp_module, "MF_MCP_URL", "https://mcp.example.com")
    http_cm, session_cls = _mock_mcp(call_text="food not found", call_is_error=True)

    with patch("mcp.client.streamable_http.streamable_http_client", http_cm), \
         patch("mcp.client.session.ClientSession", session_cls):
        result = await call_mf_tool("log_food", {"food": "???"})

    assert "error" in result
    assert "food not found" in result["error"]


async def test_call_mf_tool_returns_error_on_exception(monkeypatch):
    monkeypatch.setattr(mcp_module, "MF_MCP_URL", "https://mcp.example.com")

    with patch("mcp.client.streamable_http.streamable_http_client", side_effect=Exception("timeout")):
        result = await call_mf_tool("log_food", {})

    assert "error" in result


# ---------------------------------------------------------------------------
# _to_anthropic_tool
# ---------------------------------------------------------------------------


def test_to_anthropic_tool_basic_conversion():
    tool = _make_tool(
        "log_food",
        "Log a food entry",
        {"type": "object", "properties": {"food": {"type": "string"}}, "required": ["food"]},
    )
    result = _to_anthropic_tool(tool)
    assert result["name"] == "mf_log_food"
    assert result["description"] == "Log a food entry"
    assert result["input_schema"]["properties"]["food"]["type"] == "string"


def test_to_anthropic_tool_no_description():
    tool = _make_tool("get_context", description="")
    result = _to_anthropic_tool(tool)
    assert result["name"] == "mf_get_context"
    assert result["description"] == ""


def test_to_anthropic_tool_pydantic_schema():
    """Handles schemas returned as Pydantic models (has model_dump)."""
    schema_obj = MagicMock()
    schema_obj.model_dump.return_value = {"type": "object", "properties": {}}
    tool = MagicMock()
    tool.name = "log_weight"
    tool.description = "Log weight"
    tool.inputSchema = schema_obj
    result = _to_anthropic_tool(tool)
    assert result["input_schema"] == {"type": "object", "properties": {}}


def test_to_anthropic_tool_non_dict_schema_fallback():
    """Non-dict, non-Pydantic schema falls back to empty object schema."""
    tool = MagicMock()
    tool.name = "get_goals"
    tool.description = ""
    tool.inputSchema = 42  # neither dict nor has model_dump
    result = _to_anthropic_tool(tool)
    assert result["input_schema"] == {"type": "object", "properties": {}}
