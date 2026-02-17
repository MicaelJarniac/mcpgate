"""A stateless gateway that turns any OpenAPI spec into MCP tools on the fly."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

__all__: tuple[str, ...] = ("OpenAPIMiddleware", "create_mcp", "mcp")

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.providers.openapi import OpenAPIProvider
from httpx import AsyncClient
from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    import mcp.types as mt
    from fastmcp.tools.tool import Tool, ToolResult


class OpenAPIMiddleware(Middleware):
    """Middleware that builds per-request MCP tools from an OpenAPI spec.

    Headers extracted from each request:
        - ``x-openapi-url``: URL of the OpenAPI JSON specification.
        - ``x-api-url``: Base URL of the target API.
        - ``x-cookies`` (optional): Cookie string forwarded to the API.

    The middleware uses ``ContextVar`` to isolate the per-request
    ``OpenAPIProvider``, so concurrent requests never share state.
    """

    _provider: ContextVar[OpenAPIProvider | None] = ContextVar(
        "_provider",
        default=None,
    )

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

        cookies = headers.get("x-cookies")
        logger.debug(f"Forwarding cookies: {cookies}")

        client = AsyncClient(
            base_url=api_url,
            headers={"Cookie": cookies} if cookies else {},
        )

        try:
            spec = await client.get(openapi_url)
            spec.raise_for_status()

            provider = OpenAPIProvider(spec.json(), client=client)
            token = self._provider.set(provider)
            try:
                handler = await self._dispatch_handler(context, call_next)
                return await handler(context)
            finally:
                self._provider.reset(token)
        finally:
            await client.aclose()

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """Prepend tools from the per-request OpenAPI provider."""
        if provider := self._provider.get():
            return [*await provider.list_tools(), *await call_next(context)]
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


def create_mcp() -> FastMCP:
    """Create and return a new FastMCP instance with OpenAPI middleware."""
    server = FastMCP()
    server.add_middleware(OpenAPIMiddleware())
    return server


mcp = create_mcp()


if __name__ == "__main__":
    mcp.run(transport="http")
