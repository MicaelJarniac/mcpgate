"""Standalone investigation of mcpgate cookie isolation behavior.

This file tests mcpgate specifically, complementing test_fastmcp_cookie_isolation.py
which covered bare FastMCP.from_openapi behavior.

Key differences from the bare FastMCP case:
  - mcpgate uses a SHARED httpx.AsyncClient per (openapi_url, api_url) cache key.
  - Cookies are NOT baked into the shared client.
  - Instead, each MCP request carries an optional ``x-cookies`` header, which the
    ``_translate_cookies`` event hook translates to a ``Cookie`` header on the
    outgoing HTTP request - per request, not per client.

Consequence:
  - Two mcpgate clients with different ``x-cookies`` values each see their own
    session on the backend (proper isolation).
  - A backend ``Set-Cookie`` response does NOT automatically propagate to future
    requests - mcpgate is stateless with respect to cookie state.
  - Cookie isolation is guaranteed regardless of whether the underlying httpx
    client is shared.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import socket
    from collections.abc import AsyncIterator

import uvicorn
from fastapi import FastAPI, Response
from fastapi import Request as FastAPIRequest
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.utilities.tests import run_server_async

from mcpgate import create_mcp

# ---------------------------------------------------------------------------
# Helpers (shared with test_mcpgate.py by convention)
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


def _transport(
    mcp_url: str, api_url: str, cookies: str | None = None
) -> StreamableHttpTransport:
    """Build a StreamableHttpTransport pointing at mcpgate with given cookies."""
    headers: dict[str, str] = {
        "x-openapi-url": f"{api_url}/openapi.json",
        "x-api-url": api_url,
    }
    if cookies is not None:
        headers["x-cookies"] = cookies
    return StreamableHttpTransport(url=mcp_url, headers=headers)


# ---------------------------------------------------------------------------
# Part 1 - x-cookies isolation between two clients
# ---------------------------------------------------------------------------


async def test_two_clients_with_different_cookies_are_isolated() -> None:
    """Two mcpgate clients carrying different x-cookies headers see different sessions.

    mcpgate translates ``x-cookies`` to ``Cookie`` per HTTP request via an event
    hook on the shared httpx.AsyncClient, so each client's cookie value is
    injected independently into its own backend calls.
    """
    async with (
        _run_fastapi(_make_cookie_app()) as api_url,
        run_server_async(create_mcp()) as mcp_url,
        Client(transport=_transport(mcp_url, api_url, "session=alice")) as client_a,
        Client(transport=_transport(mcp_url, api_url, "session=bob")) as client_b,
    ):
        whoami_a, whoami_b = await asyncio.gather(
            client_a.call_tool("whoami_whoami_get"),
            client_b.call_tool("whoami_whoami_get"),
        )

        assert not whoami_a.is_error
        assert not whoami_b.is_error

        assert "alice" in str(whoami_a.content), (
            f"Client A expected 'alice', got: {whoami_a.content}"
        )
        assert "bob" in str(whoami_b.content), (
            f"Client B expected 'bob', got: {whoami_b.content}"
        )


async def test_client_without_cookies_sees_anonymous() -> None:
    """A client that sends no x-cookies header sees the unauthenticated state."""
    async with (
        _run_fastapi(_make_cookie_app()) as api_url,
        run_server_async(create_mcp()) as mcp_url,
        Client(transport=_transport(mcp_url, api_url)) as client,
    ):
        whoami = await client.call_tool("whoami_whoami_get")

        assert not whoami.is_error
        assert "anonymous" in str(whoami.content), (
            f"Expected 'anonymous' with no cookies, got: {whoami.content}"
        )


# ---------------------------------------------------------------------------
# Part 2 - Set-Cookie responses are NOT automatically persisted
# ---------------------------------------------------------------------------


async def test_login_set_cookie_not_persisted_to_next_call() -> None:
    """A Set-Cookie response from /login does NOT carry over to the next call.

    mcpgate is stateless with respect to cookie state: it only forwards what
    the MCP client explicitly passes in ``x-cookies``. Even though the shared
    httpx.AsyncClient might store the Set-Cookie in its jar, subsequent requests
    are built as raw httpx.Request objects (via RequestDirector), so jar cookies
    are never injected - the same root cause documented in
    test_fastmcp_cookie_isolation.py.
    """
    # Client sends no x-cookies at all
    async with (
        _run_fastapi(_make_cookie_app()) as api_url,
        run_server_async(create_mcp()) as mcp_url,
        Client(transport=_transport(mcp_url, api_url)) as client,
    ):
        # Login - the backend responds with Set-Cookie: session=alice
        login = await client.call_tool("login_login_post", {"username": "alice"})
        assert not login.is_error

        # Next call has no x-cookies - the Set-Cookie was NOT persisted
        whoami = await client.call_tool("whoami_whoami_get")
        assert not whoami.is_error

        assert "anonymous" in str(whoami.content), (
            "Set-Cookie from login should NOT be forwarded on the next call; "
            f"got: {whoami.content}"
        )


# ---------------------------------------------------------------------------
# Part 3 - Shared httpx client does not leak cookies across clients
# ---------------------------------------------------------------------------


async def test_shared_httpx_client_does_not_leak_cookies_between_clients() -> None:
    """The shared httpx.AsyncClient does not leak one client's cookies to another.

    Because mcpgate injects cookies from ``x-cookies`` on each raw request (not
    via the jar), even if the underlying httpx client caches a Set-Cookie in its
    jar, Client B never inherits Client A's session cookie.
    """
    async with (
        _run_fastapi(_make_cookie_app()) as api_url,
        run_server_async(create_mcp()) as mcp_url,
        Client(transport=_transport(mcp_url, api_url, "session=alice")) as client_a,
        Client(transport=_transport(mcp_url, api_url)) as client_b,
    ):
        # Client A logs in - this might write to the shared client's jar
        login_a = await client_a.call_tool(
            "login_login_post", {"username": "alice"}
        )
        assert not login_a.is_error

        # Client B has no x-cookies - should still see "anonymous"
        whoami_b = await client_b.call_tool("whoami_whoami_get")
        assert not whoami_b.is_error

        assert "anonymous" in str(whoami_b.content), (
            "Client B should not inherit Client A's cookie via the shared "
            f"httpx client, but got: {whoami_b.content}"
        )

        # Client A should still see its own cookie correctly
        whoami_a = await client_a.call_tool("whoami_whoami_get")
        assert not whoami_a.is_error

        assert "alice" in str(whoami_a.content), (
            f"Client A expected 'alice', got: {whoami_a.content}"
        )
