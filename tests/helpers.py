"""Shared test helpers for mcpgate tests and benchmarks."""

from __future__ import annotations

import asyncio
import threading
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import socket
    from collections.abc import AsyncIterator, Callable, Coroutine

import httpx
import uvicorn
from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.utilities.tests import run_server_async

from mcpgate import OpenAPIMiddleware, create_mcp


@asynccontextmanager
async def run_fastapi(
    app: FastAPI,
    host: str = "127.0.0.1",
) -> AsyncIterator[str]:
    """Run a FastAPI app in the background and yield the base URL."""
    started = asyncio.Event()
    config = uvicorn.Config(app, host=host, port=0, log_level="error")
    server = uvicorn.Server(config)
    original_startup = server.startup

    async def _startup(sockets: list[socket.socket] | None = None) -> None:
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


def make_test_app() -> FastAPI:
    """Return a minimal FastAPI app with /hello and /echo endpoints."""
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


async def make_vanilla_server(api_url: str) -> FastMCP:
    """Create a static ``FastMCP.from_openapi`` server (no mcpgate middleware)."""
    async with httpx.AsyncClient() as spec_client:
        resp = await spec_client.get(f"{api_url}/openapi.json")
        resp.raise_for_status()
        spec = resp.json()
    return FastMCP.from_openapi(spec, client=httpx.AsyncClient(base_url=api_url))


def create_mcp_with_middleware(**kwargs: Any) -> tuple[FastMCP, OpenAPIMiddleware]:  # noqa: ANN401
    """Create a FastMCP instance paired with its ``OpenAPIMiddleware``."""
    server = FastMCP()
    middleware = OpenAPIMiddleware(**kwargs)
    server.add_middleware(middleware)
    return server, middleware


@dataclass
class Servers:
    """Live server URLs plus the background thread managing them."""

    api_url: str
    mcp_url: str
    _thread: threading.Thread = field(repr=False)
    _loop: asyncio.AbstractEventLoop = field(repr=False)
    _stop: list[asyncio.Event] = field(default_factory=list, repr=False)

    def stop(self) -> None:
        """Signal shutdown and wait for the background thread to finish."""
        if self._stop:
            self._loop.call_soon_threadsafe(self._stop[0].set)
        self._thread.join(timeout=10)


def launch_servers(
    mcp_server: FastMCP | None = None,
    mcp_factory: Callable[[str], Coroutine[Any, Any, FastMCP]] | None = None,
) -> Servers:
    """Spin up FastAPI + MCP servers in a background thread and return URLs.

    Priority: *mcp_factory* (called with the live ``api_url``) > *mcp_server*
    > ``create_mcp()`` (mcpgate default).
    """
    loop = asyncio.new_event_loop()
    state: dict[str, str] = {}
    ready = threading.Event()
    stop_events: list[asyncio.Event] = []

    async def _serve() -> None:
        stop = asyncio.Event()
        stop_events.append(stop)
        async with AsyncExitStack() as stack:
            app = make_test_app()
            state["api_url"] = await stack.enter_async_context(run_fastapi(app))
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
    assert ready.wait(timeout=30), "Servers failed to start within 30 s"
    return Servers(
        api_url=state["api_url"],
        mcp_url=state["mcp_url"],
        _thread=t,
        _loop=loop,
        _stop=stop_events,
    )
