"""Testing for mcpgate."""

from __future__ import annotations

__all__: tuple[str, ...] = ()

import asyncio

import pytest
from fastapi import FastAPI
from fastapi import Request as FastAPIRequest
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.utilities.tests import run_server_async

from mcpgate import create_mcp

from .helpers import create_mcp_with_middleware, make_test_app, run_fastapi


async def test_list_tools() -> None:
    """Test that OpenAPI endpoints are exposed as MCP tools."""
    async with (
        run_fastapi(make_test_app()) as api_url,
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
            assert tool_names
            assert "hello_hello_get" in tool_names
            assert "echo_echo_post" in tool_names


async def test_call_hello() -> None:
    """Test calling the hello endpoint via MCP."""
    async with (
        run_fastapi(make_test_app()) as api_url,
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
    async with (
        run_fastapi(make_test_app()) as api_url,
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


async def test_arbitrary_headers_forwarded() -> None:
    """Test that arbitrary MCP client headers are forwarded to the API."""
    app = FastAPI()

    @app.get("/headers")
    async def headers(request: FastAPIRequest) -> dict[str, str]:
        """Return selected request headers."""
        return {
            "x-custom-foo": request.headers.get("x-custom-foo", ""),
            "x-trace-id": request.headers.get("x-trace-id", ""),
        }

    async with (
        run_fastapi(app) as api_url,
        run_server_async(create_mcp()) as mcp_url,
    ):
        transport = StreamableHttpTransport(
            url=mcp_url,
            headers={
                "x-openapi-url": f"{api_url}/openapi.json",
                "x-api-url": api_url,
                "x-custom-foo": "bar",
                "x-trace-id": "abc-123",
            },
        )
        async with Client(transport=transport) as client:
            result = await client.call_tool("headers_headers_get")
            assert not result.is_error
            assert any("bar" in str(c) for c in result.content)
            assert any("abc-123" in str(c) for c in result.content)


async def test_concurrent_requests_are_isolated() -> None:
    """Test that concurrent requests each see only their own tools."""
    app_a = FastAPI()

    @app_a.get("/alpha")
    async def alpha() -> str:
        """Return alpha."""
        return "alpha"

    app_b = FastAPI()

    @app_b.get("/beta")
    async def beta() -> str:
        """Return beta."""
        return "beta"

    async with (
        run_fastapi(app_a) as url_a,
        run_fastapi(app_b) as url_b,
        run_server_async(create_mcp()) as mcp_url,
    ):
        transport_a = StreamableHttpTransport(
            url=mcp_url,
            headers={"x-openapi-url": f"{url_a}/openapi.json", "x-api-url": url_a},
        )
        transport_b = StreamableHttpTransport(
            url=mcp_url,
            headers={"x-openapi-url": f"{url_b}/openapi.json", "x-api-url": url_b},
        )

        async def list_tools_for(transport: StreamableHttpTransport) -> set[str]:
            async with Client(transport=transport) as client:
                tools = await client.list_tools()
                return {t.name for t in tools}

        names_a, names_b = await asyncio.gather(
            list_tools_for(transport_a),
            list_tools_for(transport_b),
        )

        assert "alpha_alpha_get" in names_a
        assert "beta_beta_get" not in names_a

        assert "beta_beta_get" in names_b
        assert "alpha_alpha_get" not in names_b


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


async def test_provider_is_cached() -> None:
    """Test that repeated requests reuse the same cached provider."""
    server, middleware = create_mcp_with_middleware()

    async with (
        run_fastapi(make_test_app()) as api_url,
        run_server_async(server) as mcp_url,
    ):
        headers = {
            "x-openapi-url": f"{api_url}/openapi.json",
            "x-api-url": api_url,
        }

        async with Client(
            transport=StreamableHttpTransport(url=mcp_url, headers=headers),
        ) as client:
            await client.list_tools()

        async with Client(
            transport=StreamableHttpTransport(url=mcp_url, headers=headers),
        ) as client:
            await client.list_tools()

        assert len(middleware._cache) == 1  # noqa: SLF001


async def test_cache_expires() -> None:
    """Test that cached providers expire after the TTL."""
    server, middleware = create_mcp_with_middleware(ttl=0.1)

    async with (
        run_fastapi(make_test_app()) as api_url,
        run_server_async(server) as mcp_url,
    ):
        headers = {
            "x-openapi-url": f"{api_url}/openapi.json",
            "x-api-url": api_url,
        }

        async with Client(
            transport=StreamableHttpTransport(url=mcp_url, headers=headers),
        ) as client:
            await client.list_tools()

        first_provider = next(iter(middleware._cache.values())).provider  # noqa: SLF001

        await asyncio.sleep(0.2)

        async with Client(
            transport=StreamableHttpTransport(url=mcp_url, headers=headers),
        ) as client:
            await client.list_tools()

        second_provider = next(iter(middleware._cache.values())).provider  # noqa: SLF001
        assert first_provider is not second_provider
