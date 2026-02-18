"""ASV benchmarks for mcpgate."""

from __future__ import annotations

import asyncio
import threading
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine

import httpx
import uvicorn
from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.utilities.tests import run_server_async

from mcpgate import OpenAPIMiddleware, create_mcp

# ---------------------------------------------------------------------------
# Helpers shared across all benchmark classes
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
    """Return a minimal FastAPI app for benchmarking."""
    app = FastAPI()

    @app.get("/hello")
    async def hello() -> str:
        return "Hello, world!"

    @app.post("/echo")
    async def echo(message: str) -> str:
        return message

    return app


@dataclass
class _Servers:
    """Live server URLs plus the background thread managing them."""

    api_url: str
    mcp_url: str
    _thread: threading.Thread = field(repr=False)
    _loop: asyncio.AbstractEventLoop = field(repr=False)
    _stop: list[asyncio.Event] = field(default_factory=list, repr=False)

    def stop(self) -> None:
        if self._stop:
            self._loop.call_soon_threadsafe(self._stop[0].set)
        self._thread.join(timeout=10)


def _launch_servers(
    mcp_server: FastMCP | None = None,
    mcp_factory: Callable[[str], Coroutine[Any, Any, FastMCP]] | None = None,
) -> _Servers:
    """Spin up FastAPI + MCP servers in a background thread and return URLs.

    Priority: *mcp_factory* (called with the live api_url) > *mcp_server* >
    ``create_mcp()`` (mcpgate default).
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
    assert ready.wait(timeout=30), "Servers failed to start within 30 s"  # noqa: S101
    return _Servers(
        api_url=state["api_url"],
        mcp_url=state["mcp_url"],
        _thread=t,
        _loop=loop,
        _stop=stop_events,
    )


async def _make_vanilla_server(api_url: str) -> FastMCP:
    """Create a static ``FastMCP.from_openapi`` server (no mcpgate middleware)."""
    async with httpx.AsyncClient() as spec_client:
        resp = await spec_client.get(f"{api_url}/openapi.json")
        resp.raise_for_status()
        spec = resp.json()
    return FastMCP.from_openapi(spec, client=httpx.AsyncClient(base_url=api_url))


# ---------------------------------------------------------------------------
# Benchmark suites
# ---------------------------------------------------------------------------


class TimeVanillaFastMCP:
    """Baseline: static ``FastMCP.from_openapi`` server with no mcpgate layer.

    The spec is fetched once at setup time and the server exposes tools
    statically.  No headers are needed — this is the minimum-overhead
    reference point against which mcpgate's per-request machinery is compared.
    """

    def setup(self) -> None:
        self._servers = _launch_servers(mcp_factory=_make_vanilla_server)

    def teardown(self) -> None:
        self._servers.stop()

    def time_list_tools(self) -> None:
        """Static list_tools: no middleware, no cache lookup."""

        async def _run() -> None:
            transport = StreamableHttpTransport(url=self._servers.mcp_url)
            async with Client(transport=transport) as client:
                await client.list_tools()

        asyncio.run(_run())

    def time_call_tool(self) -> None:
        """Static call_tool: direct proxy to target API via vanilla FastMCP."""

        async def _run() -> None:
            transport = StreamableHttpTransport(url=self._servers.mcp_url)
            async with Client(transport=transport) as client:
                await client.call_tool("hello_hello_get")

        asyncio.run(_run())


class TimeCacheHit:
    """Round-trip latency when the OpenAPI provider cache is warm.

    setup() primes the cache once; all timing calls exercise the fast path.
    """

    def setup(self) -> None:
        fmcp = FastMCP()
        self._middleware = OpenAPIMiddleware()
        fmcp.add_middleware(self._middleware)
        self._servers = _launch_servers(fmcp)
        self._headers = {
            "x-openapi-url": f"{self._servers.api_url}/openapi.json",
            "x-api-url": self._servers.api_url,
        }
        # Prime the cache so all timing iterations hit the warm path.
        asyncio.run(self._list_tools())

    def teardown(self) -> None:
        self._servers.stop()

    # -- helpers (prefixed with _ so ASV ignores them) ----------------------

    async def _list_tools(self) -> None:
        transport = StreamableHttpTransport(
            url=self._servers.mcp_url, headers=self._headers
        )
        async with Client(transport=transport) as client:
            await client.list_tools()

    async def _call_tool(self) -> None:
        transport = StreamableHttpTransport(
            url=self._servers.mcp_url, headers=self._headers
        )
        async with Client(transport=transport) as client:
            await client.call_tool("hello_hello_get")

    # -- timing benchmarks --------------------------------------------------

    def time_list_tools(self) -> None:
        """Warm-cache list_tools round-trip (network + middleware dispatch)."""
        asyncio.run(self._list_tools())

    def time_call_tool(self) -> None:
        """Warm-cache call_tool round-trip (network + proxy to target API)."""
        asyncio.run(self._call_tool())


class TimeCacheMiss:
    """Round-trip latency when the provider is never cached (ttl=0).

    With ttl=0 the cache entry expires immediately, so every timing call
    exercises the full cold path: lock acquire -> spec HTTP fetch -> JSON parse
    -> OpenAPIProvider construction -> AsyncClient creation.
    """

    def setup(self) -> None:
        fmcp = FastMCP()
        fmcp.add_middleware(OpenAPIMiddleware(ttl=0))
        self._servers = _launch_servers(fmcp)
        self._headers = {
            "x-openapi-url": f"{self._servers.api_url}/openapi.json",
            "x-api-url": self._servers.api_url,
        }

    def teardown(self) -> None:
        self._servers.stop()

    def time_list_tools(self) -> None:
        """Cold-path list_tools: spec fetch + parse + provider creation."""

        async def _run() -> None:
            transport = StreamableHttpTransport(
                url=self._servers.mcp_url, headers=self._headers
            )
            async with Client(transport=transport) as client:
                await client.list_tools()

        asyncio.run(_run())


class TimeNoHeaders:
    """List-tools latency when no OpenAPI headers are sent.

    The middleware returns early with an empty tool list, exercising only
    FastMCP's own dispatch overhead.
    """

    def setup(self) -> None:
        self._servers = _launch_servers()

    def teardown(self) -> None:
        self._servers.stop()

    def time_list_tools(self) -> None:
        """No-headers fast path: middleware skips all OpenAPI logic."""

        async def _run() -> None:
            transport = StreamableHttpTransport(url=self._servers.mcp_url)
            async with Client(transport=transport) as client:
                await client.list_tools()

        asyncio.run(_run())
