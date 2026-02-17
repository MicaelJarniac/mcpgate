"""A stateless gateway that turns any OpenAPI spec into MCP tools on the fly."""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from dataclasses import dataclass
from itertools import chain
from typing import TYPE_CHECKING, Any

__all__: tuple[str, ...] = ("OpenAPIMiddleware", "create_mcp", "mcp")

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.providers.openapi import OpenAPIProvider
from httpx import AsyncClient, Request
from loguru import logger
from typer import Typer

from mcpgate.log import Level, setup_logging

if TYPE_CHECKING:
    from collections.abc import Sequence

    import mcp.types as mt
    from fastmcp.tools.tool import Tool, ToolResult


logger.disable("mcpgate")


type URL = str
type URL_OpenAPI = URL
type URL_API = URL

type CacheKey = tuple[URL_OpenAPI, URL_API]

type Seconds = float
type Epoch = Seconds


async def _translate_cookies(request: Request) -> None:
    """Translate ``x-cookies`` to ``cookie`` for backwards compatibility.

    Registered as an httpx request event hook so the translation happens
    per-request without baking cookies into the ``AsyncClient``.
    """
    if cookies := request.headers.get("x-cookies"):
        del request.headers["x-cookies"]
        request.headers["cookie"] = cookies


@dataclass(slots=True)
class _CachedProvider:
    """A cached ``OpenAPIProvider`` with its associated client and expiry."""

    provider: OpenAPIProvider
    client: AsyncClient
    expires_at: Epoch


class OpenAPIMiddleware(Middleware):
    """Middleware that builds per-request MCP tools from an OpenAPI spec.

    Headers extracted from each request:
        - ``x-openapi-url``: URL of the OpenAPI JSON specification.
        - ``x-api-url``: Base URL of the target API.
        - ``x-cookies`` (optional): Cookie string forwarded to the API.

    The middleware uses ``ContextVar`` to isolate the per-request
    ``OpenAPIProvider``, so concurrent requests never share state.

    Providers and their HTTP clients are cached by ``(openapi_url, api_url)``
    with a configurable TTL to avoid redundant spec fetches and parsing.
    """

    _provider: ContextVar[OpenAPIProvider | None] = ContextVar(
        "_provider",
        default=None,
    )

    def __init__(self, *, ttl: Seconds = 300.0) -> None:
        """Initialize the middleware with a provider cache TTL in seconds."""
        self._ttl = ttl
        self._cache: dict[CacheKey, _CachedProvider] = {}
        self._lock = asyncio.Lock()
        self._spec_client: AsyncClient | None = None

    async def _get_provider(
        self,
        openapi_url: URL_OpenAPI,
        api_url: URL_API,
    ) -> OpenAPIProvider:
        """Return a cached provider or create a new one."""
        key = (openapi_url, api_url)
        now = time.monotonic()

        # Fast path: cache hit
        cached = self._cache.get(key)
        if cached is not None and cached.expires_at > now:
            return cached.provider

        # Slow path: acquire lock, double-check, then create
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached.expires_at > now:
                return cached.provider

            # Evict expired entry
            old = self._cache.pop(key, None)
            if old is not None:
                await old.client.aclose()

            # Lazy-init the dedicated spec-fetching client
            if self._spec_client is None:
                self._spec_client = AsyncClient()

            spec = await self._spec_client.get(openapi_url)
            spec.raise_for_status()

            client = AsyncClient(
                base_url=api_url,
                event_hooks={"request": [_translate_cookies]},
            )

            try:
                provider = OpenAPIProvider(spec.json(), client=client)
            except Exception:
                await client.aclose()
                raise

            self._cache[key] = _CachedProvider(
                provider=provider,
                client=client,
                expires_at=now + self._ttl,
            )
            return provider

    async def __call__(self, context: MiddlewareContext, call_next: CallNext) -> Any:  # noqa: ANN401
        """Fetch the OpenAPI spec and dispatch to operation-specific hooks."""
        headers = get_http_headers()
        logger.debug(f"Received headers: {headers}")

        openapi_url = headers.get("x-openapi-url")
        api_url = headers.get("x-api-url")

        if not openapi_url or not api_url:
            logger.warning("No OpenAPI URL or API URL provided in headers.")
            return await call_next(context)

        logger.info("OpenAPI URL and API URL found in headers, adding provider.")

        provider = await self._get_provider(openapi_url, api_url)
        token = self._provider.set(provider)
        try:
            handler = await self._dispatch_handler(context, call_next)
            return await handler(context)
        finally:
            self._provider.reset(token)

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """Prepend tools from the per-request OpenAPI provider."""
        if provider := self._provider.get():
            return list(chain(await provider.list_tools(), await call_next(context)))
        return await call_next(context)

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Intercept tool calls destined for the per-request OpenAPI provider."""
        if provider := self._provider.get():
            tool = await provider.get_tool(context.message.name)
            if tool:
                return await tool.run(arguments=context.message.arguments or {})
        return await call_next(context)

    async def close(self) -> None:
        """Close all cached clients and the spec-fetching client."""
        for cached in self._cache.values():
            await cached.client.aclose()
        self._cache.clear()
        if self._spec_client is not None:
            await self._spec_client.aclose()
            self._spec_client = None


def create_mcp() -> FastMCP:
    """Create and return a new FastMCP instance with OpenAPI middleware."""
    server = FastMCP()
    server.add_middleware(OpenAPIMiddleware())
    return server


mcp = create_mcp()
app = Typer()


@app.command()
def run(port: int = 8000, log_level: Level = Level.INFO) -> None:
    """Run the MCP gateway server."""
    setup_logging(level=log_level)
    mcp.run(transport="http", port=port)


if __name__ == "__main__":
    app()
