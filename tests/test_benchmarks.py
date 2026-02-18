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
import threading
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine, Generator

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.utilities.tests import run_server_async

from mcpgate import OpenAPIMiddleware, create_mcp

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _run_fastapi(
    app: FastAPI,
    host: str = "127.0.0.1",
) -> AsyncIterator[str]:
    """Run a FastAPI app in the background and yield the base URL."""
    started = asyncio.Event()
    config = uvicorn.Config(app, host=host, port=0, log_level="error")
    server = uvicorn.Server(config)
    original_startup = server.startup

    async def _startup(sockets=None) -> None:  # type: ignore[no-untyped-def]
        await original_startup(sockets=sockets)
        started.set()

    server.startup = _startup  # type: ignore[assignment]
    task = asyncio.create_task(server.serve())
    await started.wait()
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        await task


def _make_test_app() -> FastAPI:
    app = FastAPI()

    @app.get("/hello")
    async def hello() -> str:
        return "Hello, world!"

    @app.post("/echo")
    async def echo(message: str) -> str:
        return message

    return app


async def _make_vanilla_server(api_url: str) -> FastMCP:
    """Create a static ``FastMCP.from_openapi`` server (no mcpgate middleware)."""
    async with httpx.AsyncClient() as spec_client:
        resp = await spec_client.get(f"{api_url}/openapi.json")
        resp.raise_for_status()
        spec = resp.json()
    return FastMCP.from_openapi(spec, client=httpx.AsyncClient(base_url=api_url))


def _start_background_servers(
    mcp_server: FastMCP | None = None,
    mcp_factory: Callable[[str], Coroutine[Any, Any, FastMCP]] | None = None,
) -> tuple[str, str, threading.Thread, asyncio.AbstractEventLoop, list[asyncio.Event]]:
    """Start FastAPI + MCP servers in a background thread.

    Priority: *mcp_factory* (called with the live api_url) > *mcp_server* >
    ``create_mcp()`` (mcpgate default).
    Returns ``(api_url, mcp_url, thread, loop, stop_events)``.
    """
    loop = asyncio.new_event_loop()
    state: dict[str, str] = {}
    ready = threading.Event()
    stop_events: list[asyncio.Event] = []

    async def _serve() -> None:
        stop = asyncio.Event()
        stop_events.append(stop)
        async with AsyncExitStack() as stack:
            app = _make_test_app()
            state["api_url"] = await stack.enter_async_context(_run_fastapi(app))
            if mcp_factory is not None:
                _server = await mcp_factory(state["api_url"])
            elif mcp_server is not None:
                _server = mcp_server
            else:
                _server = create_mcp()
            state["mcp_url"] = await stack.enter_async_context(
                run_server_async(_server)
            )
            ready.set()
            await stop.wait()

    t = threading.Thread(target=lambda: loop.run_until_complete(_serve()), daemon=True)
    t.start()
    assert ready.wait(timeout=30), "Servers failed to start"
    return state["api_url"], state["mcp_url"], t, loop, stop_events


def _make_fixture(
    mcp_server: FastMCP | None = None,
    mcp_factory: Callable[[str], Coroutine[Any, Any, FastMCP]] | None = None,
) -> Generator[tuple[str, str]]:
    api_url, mcp_url, t, loop, stop_events = _start_background_servers(
        mcp_server=mcp_server, mcp_factory=mcp_factory
    )
    yield api_url, mcp_url
    if stop_events:
        loop.call_soon_threadsafe(stop_events[0].set)
    t.join(timeout=10)


# ---------------------------------------------------------------------------
# Module-scoped fixtures (servers stay alive for the whole module)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_servers() -> Generator[tuple[str, str]]:
    """Yield ``(api_url, mcp_url)`` for a warm-cache MCP gateway."""
    yield from _make_fixture()


@pytest.fixture(scope="module")
def live_servers_cold() -> Generator[tuple[str, str]]:
    """Yield ``(api_url, mcp_url)`` for a zero-TTL (always-cold) gateway."""
    fmcp = FastMCP()
    fmcp.add_middleware(OpenAPIMiddleware(ttl=0))
    yield from _make_fixture(mcp_server=fmcp)


@pytest.fixture(scope="module")
def live_servers_vanilla() -> Generator[tuple[str, str]]:
    """Yield ``(api_url, mcp_url)`` for a static ``FastMCP.from_openapi`` server."""
    yield from _make_fixture(mcp_factory=_make_vanilla_server)


# ---------------------------------------------------------------------------
# Benchmark helpers
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
