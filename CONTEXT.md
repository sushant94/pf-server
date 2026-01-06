# PF-Server Context Dump

This document captures all design decisions, architecture choices, and context from the initial planning session.

## Overview

A server + CLI system for a **closed beta with ~5 known users**. Users authenticate via GitHub OAuth, then communicate with per-user Docker containers via WebSocket.

## Architecture

```
CLI                         Server                    GitHub
 │                            │                          │
 │  ┌─────────────────────────────────────────────────────────────┐
 │  │ PHASE 1: OAuth (one-time login)                             │
 │  └─────────────────────────────────────────────────────────────┘
 │── open browser ────────────────────────────────────────►│
 │◄── redirect localhost/callback?code=xxx ──────────────│
 │                            │                          │
 │── POST /auth/token {code} ─►│                          │
 │                            │── exchange code ─────────►│
 │                            │◄── github_token ─────────│
 │                            │── GET /user ─────────────►│
 │                            │◄── {id, login} ──────────│
 │                            │                          │
 │                            │ ✓ check whitelist        │
 │                            │ ✓ ensure container       │
 │                            │ ✓ issue JWT              │
 │◄── {jwt} ──────────────────│                          │
 │                            │                          │
 │ save ~/.config/pf/token    │                          │
 │                            │                          │
 │  ┌─────────────────────────────────────────────────────────────┐
 │  │ PHASE 2: WebSocket (all subsequent communication)           │
 │  └─────────────────────────────────────────────────────────────┘
 │── WS /connect?token={jwt} ─►│                          │
 │                            │ ✓ verify JWT (auth gate) │
 │                            │ ✓ get user's container   │
 │◄═══════════ WS bidirectional proxy ═══════════════════►│ Container
```

## Design Decisions

### 1. Authentication Pattern: Simple Token Exchange (NOT full BFF)

**Decision**: Use simple token exchange pattern instead of full BFF (Backend-for-Frontend).

**Rationale**:
- Full BFF is overkill for 5-user closed beta
- Client is a CLI (not browser), so no XSS risk
- No cookies needed, no CSRF concern
- Simpler implementation (~200 lines total)

**Flow**:
1. Client does OAuth with GitHub
2. Client sends GitHub code to server ONCE
3. Server validates with GitHub, checks whitelist, issues its own JWT
4. Client uses server's JWT for all subsequent requests

### 2. OAuth Callback Handling

**Decision**: CLI runs temporary local HTTP server to catch OAuth redirect.

**Alternatives considered**:
- Device flow (user enters code manually) - rejected, worse UX

**Implementation**: CLI starts server on `localhost:9876`, catches `/callback?code=xxx`

### 3. Token Storage

**Decision**: Plain file at `~/.config/pf/token` with `600` permissions.

**Alternatives considered**:
- OS keychain via `keyring` library - rejected, adds complexity for beta

### 4. WebSocket Connection

**Decision**: Proxy through server (containers stay internal).

**Alternatives considered**:
- Direct client-to-container connection - rejected, would require exposing container ports

**Benefits**:
- Containers on internal Docker network, not exposed
- Server handles auth verification
- Single entry point

### 5. User Whitelist

**Decision**: Simple set of GitHub user IDs in config/env.

**Key insight**: Use GitHub's numeric `id` (immutable), NOT `login` (can be changed by user).

```python
ALLOWED_GITHUB_IDS = {12345, 67890}
```

### 6. Container Management

**Decision**: One container per user, named `pf-user-{github_id}`.

**Behavior**:
- On auth: ensure container exists (create if not)
- On connect: get or start container, proxy WebSocket to it
- Containers on internal Docker network (`pf-internal`)

### 7. Project Structure

**Decision**: Separate repository using `uv` for dependency management.

```
pf-server/
├── pyproject.toml
├── .env.example
├── src/pf_server/
│   ├── __init__.py
│   ├── main.py        # FastAPI app + routes
│   ├── config.py      # Pydantic settings
│   ├── auth.py        # JWT + GitHub OAuth
│   ├── containers.py  # Docker management
│   └── ws_proxy.py    # WebSocket proxy
└── cli/
    ├── __init__.py
    └── pf_cli.py      # login + connect commands
```

## Security Analysis (for CLI client)

### Threats That Don't Apply

| Threat | Why N/A |
|--------|---------|
| XSS | No browser, no JavaScript |
| CSRF | No cookies |
| Token theft via browser | No browser |

### Actual Threats

| Threat | Mitigation |
|--------|------------|
| Token stolen from disk | File permissions `600` |
| MITM | HTTPS |
| User shares token | Trusted beta users |

**Conclusion**: For 5 trusted users in closed beta, this is secure enough.

## Dependencies

```
# Server
fastapi
uvicorn[standard]
httpx
python-jose[cryptography]
pydantic-settings
docker
websockets

# CLI
httpx
websockets

# Dev
ruff  (to be added)
```

## API Endpoints

| Endpoint | Type | Purpose |
|----------|------|---------|
| `GET /auth/login` | HTTP | Returns GitHub OAuth URL |
| `POST /auth/token` | HTTP | Exchange code → JWT (+ ensure container) |
| `WS /connect?token=` | WebSocket | Authenticated proxy to user's container |

## Environment Variables

```
GITHUB_CLIENT_ID=xxx
GITHUB_CLIENT_SECRET=xxx
JWT_SECRET=xxx
ALLOWED_GITHUB_IDS=12345,67890
CONTAINER_IMAGE=pf-user-container:latest
CONTAINER_NETWORK=pf-internal
```

## CLI Commands

```bash
python cli/pf_cli.py login    # OAuth flow, saves JWT to ~/.config/pf/token
python cli/pf_cli.py connect  # WebSocket connection to user's container
```

## Server Startup

```bash
uvicorn pf_server.main:app --reload
```

## Remaining Setup Tasks

1. Add `ruff` as dev dependency and configure in `pyproject.toml`
2. Update `README.md` with quick guide + architecture diagram
3. Create `CLAUDE.md` based on pytest-property-checker's version
4. Run `ruff format` on all files

## Key Libraries & Why

| Library | Purpose |
|---------|---------|
| `docker` | Official Docker SDK - container lifecycle |
| `python-jose` | JWT creation/verification |
| `httpx` | Async HTTP client for GitHub API |
| `websockets` | WebSocket client for proxying to containers |
| `pydantic-settings` | Type-safe config from env vars |
| `fastapi` | Web framework with native WebSocket support |

## References from Research

- [BFF Pattern - Auth0](https://auth0.com/blog/the-backend-for-frontend-pattern-bff/)
- [OAuth2 Browser Apps - IETF](https://datatracker.ietf.org/doc/html/draft-ietf-oauth-browser-based-apps)
- [Docker SDK for Python](https://docker-py.readthedocs.io/en/stable/)
- [oauth2-proxy](https://github.com/oauth2-proxy/oauth2-proxy) - similar pattern
- [JupyterHub](https://github.com/jupyterhub/zero-to-jupyterhub-k8s) - per-user containers
