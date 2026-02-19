"""ASV benchmarks for mcpgate."""

from __future__ import annotations

import asyncio

from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport

from mcpgate import OpenAPIMiddleware

from .helpers import Servers, launch_servers, make_vanilla_server

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
        """Start the vanilla FastMCP server."""
        self._servers: Servers = launch_servers(mcp_factory=make_vanilla_server)

    def teardown(self) -> None:
        """Stop the benchmark server."""
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
        """Start the mcpgate server and prime the provider cache."""
        fmcp = FastMCP()
        self._middleware = OpenAPIMiddleware()
        fmcp.add_middleware(self._middleware)
        self._servers: Servers = launch_servers(fmcp)
        self._headers = {
            "x-openapi-url": f"{self._servers.api_url}/openapi.json",
            "x-api-url": self._servers.api_url,
        }
        # Prime the cache so all timing iterations hit the warm path.
        asyncio.run(self._list_tools())

    def teardown(self) -> None:
        """Stop the benchmark server."""
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
        """Start the mcpgate server with TTL=0 (no caching)."""
        fmcp = FastMCP()
        fmcp.add_middleware(OpenAPIMiddleware(ttl=0))
        self._servers: Servers = launch_servers(fmcp)
        self._headers = {
            "x-openapi-url": f"{self._servers.api_url}/openapi.json",
            "x-api-url": self._servers.api_url,
        }

    def teardown(self) -> None:
        """Stop the benchmark server."""
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
        """Start the default mcpgate server."""
        self._servers: Servers = launch_servers()

    def teardown(self) -> None:
        """Stop the benchmark server."""
        self._servers.stop()

    def time_list_tools(self) -> None:
        """No-headers fast path: middleware skips all OpenAPI logic."""

        async def _run() -> None:
            transport = StreamableHttpTransport(url=self._servers.mcp_url)
            async with Client(transport=transport) as client:
                await client.list_tools()

        asyncio.run(_run())
