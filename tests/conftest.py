"""Pytest configuration and shared fixtures for mcpgate tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

import pytest
from fastmcp import FastMCP

from mcpgate import OpenAPIMiddleware

from .helpers import Servers, launch_servers, make_vanilla_server


def _fixture_from_servers(servers: Servers) -> Generator[tuple[str, str]]:
    yield servers.api_url, servers.mcp_url
    servers.stop()


@pytest.fixture(scope="module")
def live_servers() -> Generator[tuple[str, str]]:
    """Yield ``(api_url, mcp_url)`` for a warm-cache mcpgate server."""
    yield from _fixture_from_servers(launch_servers())


@pytest.fixture(scope="module")
def live_servers_cold() -> Generator[tuple[str, str]]:
    """Yield ``(api_url, mcp_url)`` for a zero-TTL (always-cold) mcpgate server."""
    fmcp = FastMCP()
    fmcp.add_middleware(OpenAPIMiddleware(ttl=0))
    yield from _fixture_from_servers(launch_servers(mcp_server=fmcp))


@pytest.fixture(scope="module")
def live_servers_vanilla() -> Generator[tuple[str, str]]:
    """Yield ``(api_url, mcp_url)`` for a static ``FastMCP.from_openapi`` server."""
    yield from _fixture_from_servers(launch_servers(mcp_factory=make_vanilla_server))
