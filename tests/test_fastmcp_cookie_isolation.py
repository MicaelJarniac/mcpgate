"""Standalone investigation of FastMCP and httpx cookie behavior.

This file does NOT use mcpgate. It exists purely to document how FastMCP's
OpenAPI proxy and httpx behave with respect to cookies.

Root cause (discovered by testing httpx directly):
  - `client.request()` / `client.post()` etc. -> automatically apply the
    cookie jar to outgoing requests AND store Set-Cookie from responses.
  - `client.send(raw_httpx_Request)` -> stores Set-Cookie from responses into
    the jar, but does NOT inject jar cookies back into pre-built requests.

FastMCP's OpenAPITool.run() uses the second pattern:
  1. RequestDirector.build() constructs a raw httpx.Request object.
  2. client.send(request) sends it.

Consequence: cookies set by the backend during one tool call are never
forwarded in subsequent tool calls, regardless of whether two FastMCP
Client instances share the same httpx.AsyncClient.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import socket
    from collections.abc import AsyncIterator
    from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Response
from fastapi import Request as FastAPIRequest
from fastmcp import FastMCP
from fastmcp.client import Client

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _run_fastapi(
    app: FastAPI,
    host: str = "127.0.0.1",
) -> AsyncIterator[str]:
    """Run a FastAPI app in the background and yield its base URL."""
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


def _make_cookie_app() -> FastAPI:
    """Return a FastAPI that issues and reads a session cookie."""
    app = FastAPI()

    @app.post("/login")
    async def login(username: str, response: Response) -> dict[str, str]:
        """Set a session cookie identifying the caller."""
        response.set_cookie("session", username)
        return {"logged_in_as": username}

    @app.get("/whoami")
    async def whoami(request: FastAPIRequest) -> dict[str, str]:
        """Return the session cookie value received by the server."""
        return {"user": request.cookies.get("session", "anonymous")}

    return app


async def _fetch_openapi_spec(base_url: str) -> dict[str, Any]:
    async with httpx.AsyncClient() as http:
        resp = await http.get(f"{base_url}/openapi.json")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Part 1 - httpx mechanics (no FastMCP involved)
# ---------------------------------------------------------------------------


async def test_httpx_client_request_applies_jar_cookies() -> None:
    """client.request() automatically applies jar cookies to each request.

    This is the baseline: using the high-level httpx API preserves session
    cookies across calls as expected.
    """
    async with (
        _run_fastapi(_make_cookie_app()) as base_url,
        httpx.AsyncClient(base_url=base_url) as client,
    ):
        await client.post("/login", params={"username": "alice"})

        assert client.cookies.get("session") == "alice", (
            "Cookie jar should contain the session cookie after login"
        )

        r = await client.get("/whoami")
        assert r.json()["user"] == "alice", (
            "Server should receive the cookie when using client.request()"
        )


async def test_httpx_client_send_stores_but_does_not_inject_cookies() -> None:
    """client.send(raw_request) stores response cookies but does NOT inject them.

    This is the critical httpx quirk that affects FastMCP:
    - Set-Cookie from the response IS stored in the client's cookie jar.
    - A subsequent raw httpx.Request sent via client.send() does NOT receive
      cookies from the jar (because cookie injection happens during
      client.build_request(), not during client.send()).
    """
    async with (
        _run_fastapi(_make_cookie_app()) as base_url,
        httpx.AsyncClient(base_url=base_url) as client,
    ):
        login_req = httpx.Request(
            "POST", f"{base_url}/login", params={"username": "alice"}
        )
        await client.send(login_req)

        # The jar does contain the cookie ...
        assert client.cookies.get("session") == "alice", (
            "Cookie jar should be populated from the Set-Cookie response header"
        )

        # ... but a new raw Request does not automatically receive it.
        whoami_req = httpx.Request("GET", f"{base_url}/whoami")
        r = await client.send(whoami_req)
        assert r.json()["user"] == "anonymous", (
            "Server should NOT receive a cookie when using client.send() "
            "with a raw httpx.Request - the jar is not injected automatically"
        )


# ---------------------------------------------------------------------------
# Part 2 - FastMCP consequence
# ---------------------------------------------------------------------------


async def test_fastmcp_cookies_not_forwarded_between_tool_calls() -> None:
    """FastMCP's OpenAPI proxy does not forward cookies between tool calls.

    OpenAPITool.run() builds a raw httpx.Request via RequestDirector.build()
    and sends it with client.send(). Because of the httpx behavior shown in
    test_httpx_client_send_stores_but_does_not_inject_cookies, a session
    cookie set by /login is never sent back on the next call to /whoami.
    """
    async with _run_fastapi(_make_cookie_app()) as api_url:
        spec = await _fetch_openapi_spec(api_url)
        mcp = FastMCP.from_openapi(
            openapi_spec=spec,
            client=httpx.AsyncClient(base_url=api_url),
        )

        async with Client(mcp) as client:
            login = await client.call_tool("login_login_post", {"username": "alice"})
            assert not login.is_error

            whoami = await client.call_tool("whoami_whoami_get")
            assert not whoami.is_error

            # The backend never receives the cookie - each tool call is a
            # fresh request with no cookies attached.
            assert "anonymous" in str(whoami.content), (
                "FastMCP should not forward cookies between tool calls, "
                f"but got: {whoami.content}"
            )


async def test_two_fastmcp_clients_with_shared_httpx_do_not_share_session() -> None:
    """Two FastMCP Client instances sharing one httpx.AsyncClient see no cookies.

    Because FastMCP never injects jar cookies into its outgoing requests,
    it does not matter whether Client A and Client B share an httpx client
    or each get their own - neither will observe the other's session.
    """
    async with _run_fastapi(_make_cookie_app()) as api_url:
        spec = await _fetch_openapi_spec(api_url)
        shared_http = httpx.AsyncClient(base_url=api_url)
        mcp = FastMCP.from_openapi(openapi_spec=spec, client=shared_http)

        async with Client(mcp) as client_a, Client(mcp) as client_b:
            await client_a.call_tool("login_login_post", {"username": "alice"})
            await client_b.call_tool("login_login_post", {"username": "bob"})

            whoami_a = await client_a.call_tool("whoami_whoami_get")
            whoami_b = await client_b.call_tool("whoami_whoami_get")

            assert not whoami_a.is_error
            assert not whoami_b.is_error

            # Neither client sees its own session - both get "anonymous"
            # because the shared jar is populated but never injected.
            assert "anonymous" in str(whoami_a.content), (
                f"Client A expected 'anonymous', got: {whoami_a.content}"
            )
            assert "anonymous" in str(whoami_b.content), (
                f"Client B expected 'anonymous', got: {whoami_b.content}"
            )


async def test_two_fastmcp_clients_with_separate_httpx_also_see_no_cookies() -> None:
    """Separate httpx clients per FastMCP instance also yield no cookies.

    Even when each FastMCP server gets its own httpx.AsyncClient - giving
    each its own cookie jar - the result is the same: no cookies are ever
    forwarded, because the raw-request / send() pattern doesn't inject them.

    This test confirms that cookie isolation between clients is not the
    interesting question; the interesting fact is that NO cookies propagate
    in either configuration.
    """
    async with _run_fastapi(_make_cookie_app()) as api_url:
        spec = await _fetch_openapi_spec(api_url)

        http_a = httpx.AsyncClient(base_url=api_url)
        http_b = httpx.AsyncClient(base_url=api_url)
        mcp_a = FastMCP.from_openapi(openapi_spec=spec, client=http_a)
        mcp_b = FastMCP.from_openapi(openapi_spec=spec, client=http_b)

        async with Client(mcp_a) as client_a, Client(mcp_b) as client_b:
            await client_a.call_tool("login_login_post", {"username": "alice"})
            await client_b.call_tool("login_login_post", {"username": "bob"})

            whoami_a = await client_a.call_tool("whoami_whoami_get")
            whoami_b = await client_b.call_tool("whoami_whoami_get")

            assert not whoami_a.is_error
            assert not whoami_b.is_error

            assert "anonymous" in str(whoami_a.content), (
                f"Client A expected 'anonymous', got: {whoami_a.content}"
            )
            assert "anonymous" in str(whoami_b.content), (
                f"Client B expected 'anonymous', got: {whoami_b.content}"
            )

        await http_a.aclose()
        await http_b.aclose()
