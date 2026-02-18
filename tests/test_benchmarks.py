"""pytest-benchmark tests for mcpgate.

Each test measures a full client-to-server round-trip.  Servers run in a
dedicated background thread so the per-call ``asyncio.run()`` overhead is
limited to client-side event-loop creation (~microseconds), which is negligible
compared to network round-trips.

Run with::

    pytest tests/test_benchmarks.py --benchmark-only
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport

# Fixtures (live_servers, live_servers_cold, live_servers_vanilla) are
# provided by tests/conftest.py and injected automatically by pytest.


# ---------------------------------------------------------------------------
# Local async helpers
# ---------------------------------------------------------------------------


async def _list_tools(mcp_url: str, headers: dict[str, str] | None = None) -> None:
    transport = StreamableHttpTransport(url=mcp_url, headers=headers or {})
    async with Client(transport=transport) as client:
        await client.list_tools()


async def _call_tool(
    mcp_url: str,
    tool: str,
    headers: dict[str, str] | None = None,
    args: dict[str, object] | None = None,
) -> None:
    transport = StreamableHttpTransport(url=mcp_url, headers=headers or {})
    async with Client(transport=transport) as client:
        await client.call_tool(tool, args or {})


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


def test_list_tools_vanilla(
    benchmark: pytest.FixtureRequest,
    live_servers_vanilla: tuple[str, str],
) -> None:
    """Baseline: static FastMCP.from_openapi, no mcpgate middleware."""
    _, mcp_url = live_servers_vanilla

    def _run() -> None:
        asyncio.run(_list_tools(mcp_url))

    benchmark(_run)


def test_call_tool_vanilla(
    benchmark: pytest.FixtureRequest,
    live_servers_vanilla: tuple[str, str],
) -> None:
    """Baseline call_tool via static FastMCP.from_openapi."""
    _, mcp_url = live_servers_vanilla

    def _run() -> None:
        asyncio.run(_call_tool(mcp_url, "hello_hello_get"))

    benchmark(_run)


def test_list_tools_cache_hit(
    benchmark: pytest.FixtureRequest,
    live_servers: tuple[str, str],
) -> None:
    """Warm-cache list_tools: measures mcpgate middleware fast path + network."""
    api_url, mcp_url = live_servers
    headers = {
        "x-openapi-url": f"{api_url}/openapi.json",
        "x-api-url": api_url,
    }
    asyncio.run(_list_tools(mcp_url, headers))  # prime the cache

    def _run() -> None:
        asyncio.run(_list_tools(mcp_url, headers))

    benchmark(_run)


def test_call_tool_cache_hit(
    benchmark: pytest.FixtureRequest,
    live_servers: tuple[str, str],
) -> None:
    """Warm-cache call_tool: measures full proxy round-trip via mcpgate."""
    api_url, mcp_url = live_servers
    headers = {
        "x-openapi-url": f"{api_url}/openapi.json",
        "x-api-url": api_url,
    }
    asyncio.run(_list_tools(mcp_url, headers))  # ensure cache is warm

    def _run() -> None:
        asyncio.run(_call_tool(mcp_url, "hello_hello_get", headers))

    benchmark(_run)


def test_list_tools_cache_miss(
    benchmark: pytest.FixtureRequest,
    live_servers_cold: tuple[str, str],
) -> None:
    """Cold-path list_tools: spec fetch + JSON parse + provider creation per call."""
    api_url, mcp_url = live_servers_cold
    headers = {
        "x-openapi-url": f"{api_url}/openapi.json",
        "x-api-url": api_url,
    }

    def _run() -> None:
        asyncio.run(_list_tools(mcp_url, headers))

    benchmark(_run)


def test_list_tools_no_headers(
    benchmark: pytest.FixtureRequest,
    live_servers: tuple[str, str],
) -> None:
    """No-headers fast path: middleware skips all OpenAPI logic immediately."""
    _, mcp_url = live_servers

    def _run() -> None:
        asyncio.run(_list_tools(mcp_url))

    benchmark(_run)
