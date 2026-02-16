"""A stateless gateway that turns any OpenAPI spec into MCP tools on the fly."""

from __future__ import annotations

from typing import Any

__all__: tuple[str, ...] = ("OpenAPIMiddleware", "create_mcp", "mcp")

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.providers import FastMCPProvider
from httpx import AsyncClient
from loguru import logger


class OpenAPIMiddleware(Middleware):
    """Middleware to extract OpenAPI spec URL and API URL from headers.

    Note: This middleware mutates the shared ``mcp.providers`` list for each
    request.  Under concurrent requests, one request's dynamically-added
    provider is briefly visible to other in-flight requests.  This is a known
    limitation of the current design and would require per-request provider
    isolation in FastMCP to fix properly.
    """

    def __init__(self, mcp: FastMCP) -> None:
        super().__init__()
        self.mcp = mcp

    async def __call__(self, context: MiddlewareContext, call_next: CallNext) -> Any:  # noqa: ANN401
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

            provider = FastMCPProvider(
                FastMCP.from_openapi(spec.json(), client=client),
            )
            self.mcp.add_provider(provider)

            try:
                return await call_next(context)
            finally:
                self.mcp.providers.remove(provider)
        finally:
            await client.aclose()


def create_mcp() -> FastMCP:
    """Create and return a new FastMCP instance with OpenAPI middleware."""
    server = FastMCP()
    server.add_middleware(OpenAPIMiddleware(server))
    return server


mcp = create_mcp()


if __name__ == "__main__":
    mcp.run(transport="http")
