"""Testing for mcpgate."""

from __future__ import annotations

__all__: tuple[str, ...] = ()

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import socket
    from collections.abc import AsyncIterator

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi import Request as FastAPIRequest
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.utilities.tests import run_server_async

from mcpgate import create_mcp


@asynccontextmanager
async def run_fastapi(
    app: FastAPI,
    host: str = "127.0.0.1",
) -> AsyncIterator[str]:
    """Run a FastAPI app in the background and yield the base URL."""
    started = asyncio.Event()
    config = uvicorn.Config(app, host=host, port=0, log_level="error")
    server = uvicorn.Server(config)
    _original_startup = server.startup

    async def _notify_startup(
        sockets: list[socket.socket] | None = None,
    ) -> None:
        await _original_startup(sockets=sockets)
        started.set()

    server.startup = _notify_startup  # type: ignore[assignment]
    task = asyncio.create_task(server.serve())
    await started.wait()
    listeners = server.servers[0].sockets
    port = listeners[0].getsockname()[1]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        await task


def _make_test_app() -> FastAPI:
    """Create a FastAPI app for testing."""
    app = FastAPI()

    @app.get("/hello")
    async def hello() -> str:
        """Return a greeting."""
        return "Hello, world!"

    @app.post("/echo")
    async def echo(message: str) -> str:
        """Echo the received message."""
        return message

    return app


async def test_list_tools() -> None:
    """Test that OpenAPI endpoints are exposed as MCP tools."""
    app = _make_test_app()

    async with (
        run_fastapi(app) as api_url,
        run_server_async(create_mcp()) as mcp_url,
    ):
        transport = StreamableHttpTransport(
            url=mcp_url,
            headers={
                "x-openapi-url": f"{api_url}/openapi.json",
                "x-api-url": api_url,
            },
        )
        async with Client(transport=transport) as client:
            tools = await client.list_tools()
            tool_names = {t.name for t in tools}
            assert len(tool_names) > 0
            assert "hello_hello_get" in tool_names
            assert "echo_echo_post" in tool_names


async def test_call_hello() -> None:
    """Test calling the hello endpoint via MCP."""
    app = _make_test_app()

    async with (
        run_fastapi(app) as api_url,
        run_server_async(create_mcp()) as mcp_url,
    ):
        transport = StreamableHttpTransport(
            url=mcp_url,
            headers={
                "x-openapi-url": f"{api_url}/openapi.json",
                "x-api-url": api_url,
            },
        )
        async with Client(transport=transport) as client:
            result = await client.call_tool("hello_hello_get")
            assert not result.is_error
            assert any("Hello, world!" in str(c) for c in result.content)


async def test_no_headers() -> None:
    """Test that requests without OpenAPI headers return no tools."""
    async with run_server_async(create_mcp()) as mcp_url:
        transport = StreamableHttpTransport(url=mcp_url)
        async with Client(transport=transport) as client:
            tools = await client.list_tools()
            assert len(tools) == 0


async def test_call_echo() -> None:
    """Test calling the echo POST endpoint via MCP."""
    app = _make_test_app()

    async with (
        run_fastapi(app) as api_url,
        run_server_async(create_mcp()) as mcp_url,
    ):
        transport = StreamableHttpTransport(
            url=mcp_url,
            headers={
                "x-openapi-url": f"{api_url}/openapi.json",
                "x-api-url": api_url,
            },
        )
        async with Client(transport=transport) as client:
            result = await client.call_tool(
                "echo_echo_post",
                {"message": "test message"},
            )
            assert not result.is_error
            assert any("test message" in str(c) for c in result.content)


async def test_cookie_forwarding() -> None:
    """Test that x-cookies header is forwarded to the API."""
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(request: FastAPIRequest) -> str:
        """Return the cookie header value."""
        return request.headers.get("cookie", "no-cookie")

    async with (
        run_fastapi(app) as api_url,
        run_server_async(create_mcp()) as mcp_url,
    ):
        transport = StreamableHttpTransport(
            url=mcp_url,
            headers={
                "x-openapi-url": f"{api_url}/openapi.json",
                "x-api-url": api_url,
                "x-cookies": "session=abc123",
            },
        )
        async with Client(transport=transport) as client:
            result = await client.call_tool("whoami_whoami_get")
            assert not result.is_error
            assert any("session=abc123" in str(c) for c in result.content)


async def test_bad_openapi_url() -> None:
    """Test that an unreachable OpenAPI URL results in an error."""
    async with run_server_async(create_mcp()) as mcp_url:
        transport = StreamableHttpTransport(
            url=mcp_url,
            headers={
                "x-openapi-url": "http://127.0.0.1:1/nonexistent",
                "x-api-url": "http://127.0.0.1:1",
            },
        )
        with pytest.raises(Exception):  # noqa: B017, PT011
            async with Client(transport=transport) as client:
                await client.list_tools()


async def test_invalid_openapi_spec() -> None:
    """Test that an invalid OpenAPI spec results in an error."""
    app = FastAPI()

    @app.get("/bad-spec")
    async def bad_spec() -> dict[str, str]:
        """Return an invalid OpenAPI spec."""
        return {"not": "an openapi spec"}

    async with (
        run_fastapi(app) as api_url,
        run_server_async(create_mcp()) as mcp_url,
    ):
        transport = StreamableHttpTransport(
            url=mcp_url,
            headers={
                "x-openapi-url": f"{api_url}/bad-spec",
                "x-api-url": api_url,
            },
        )
        with pytest.raises(Exception):  # noqa: B017, PT011
            async with Client(transport=transport) as client:
                await client.list_tools()
