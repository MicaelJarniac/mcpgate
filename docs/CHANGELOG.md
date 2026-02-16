# CHANGELOG


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
