# CHANGELOG


## v0.2.0 (2026-02-17)

### Features

- Add CLI entrypoint for `uvx mcpgate` / `pipx run mcpgate`
  ([`923a400`](https://github.com/MicaelJarniac/mcpgate/commit/923a400386397f64a9433d1dfea11897b478e801))

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>

### Refactoring

- Concurrency-safe middleware via dispatch hooks + ContextVar
  ([#1](https://github.com/MicaelJarniac/mcpgate/pull/1),
  [`e60059d`](https://github.com/MicaelJarniac/mcpgate/commit/e60059d456521c931c4541efc3a21c22b0f57287))

* refactor: use middleware dispatch hooks + ContextVar for concurrency safety

Replace the shared-providers-list mutation pattern with FastMCP's middleware dispatch hooks
  (on_list_tools, on_call_tool) and a ContextVar to store the per-request OpenAPIProvider.

This eliminates the race condition where concurrent requests could see each other's
  dynamically-added providers. The new approach:

- __call__: fetches the spec, stores OpenAPIProvider in a ContextVar, then delegates to the dispatch
  chain - on_list_tools: reads the ContextVar and prepends per-request tools - on_call_tool:
  intercepts calls to per-request tools directly

No shared state is mutated. Follows the same pattern as FastMCP's own ToolInjectionMiddleware.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>

* test: add concurrent request isolation test

Spins up two different FastAPI apps against the same MCP server and uses asyncio.gather to list
  tools concurrently, asserting each client sees only its own API's tools.

* chore: simplify

* test: verify arbitrary MCP client headers are forwarded to the API

FastMCP's OpenAPITool.run() calls get_http_headers() and merges them into outgoing requests. This
  test confirms custom headers (x-custom-foo, x-trace-id) sent by the MCP client reach the target
  API.

---------

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>


## v0.1.1 (2026-02-16)

### Bug Fixes

- Close AsyncClient, guard provider cleanup with try/finally, and expand tests
  ([`2e8c520`](https://github.com/MicaelJarniac/mcpgate/commit/2e8c5208b99299645208d36a6e041042baf75230))

- Wrap provider lifecycle in nested try/finally to prevent resource leaks (AsyncClient) and provider
  leaks on errors - Remove dead TYPE_CHECKING import of Any - Document known concurrency limitation
  on shared providers list - Fix wrong usage example in README and add headers protocol docs - Add
  tests for echo endpoint, cookie forwarding, bad URL, and invalid spec

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>

### Testing

- Add integration tests for OpenAPI-to-MCP gateway
  ([`79ecdfc`](https://github.com/MicaelJarniac/mcpgate/commit/79ecdfcda48fbb8367b04c41bf2e05433c4ca6f8))

Replace dummy test with real async integration tests that spin up both a FastAPI server and the
  mcpgate MCP server, verifying tool listing, tool invocation, and graceful handling of missing
  headers. Extract create_mcp() factory to avoid event-loop binding issues across tests.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>


## v0.1.0 (2026-02-16)

### Features

- Basic functionality
  ([`58e2b09`](https://github.com/MicaelJarniac/mcpgate/commit/58e2b09eb3ed582a289e6a42bfdee1e16ea4c7ef))


## v0.0.0 (2026-02-15)
