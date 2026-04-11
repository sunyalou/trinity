---
name: validate-config
description: Validate config hygiene — docker-compose env vars vs .env.example vs code references vs architecture docs. Flags missing, stale, or undocumented configuration.
allowed-tools: [Read, Grep, Glob, Bash]
user-invocable: true
---

# Validate Config

## Purpose

Check that configuration is consistent across all surfaces: docker-compose files, `.env.example`, backend code that reads env vars, and documentation. Report gaps where a variable is referenced but not documented, or documented but unused. No changes are made — read-only analysis.

## State Dependencies

| Source | Location | Read | Write | Description |
|--------|----------|------|-------|-------------|
| Docker Compose (local) | `docker-compose.yml` | R | | Local service definitions |
| Docker Compose (prod) | `docker-compose.prod.yml` | R | | Production service definitions |
| Env example | `.env.example` | R | | Documented env vars |
| Backend config | `src/backend/config.py` | R | | Centralized config constants |
| Backend code | `src/backend/` | R | | os.environ / os.getenv usage |
| MCP server code | `src/mcp-server/src/` | R | | process.env usage |
| Frontend code | `src/frontend/` | R | | VITE_ env var usage |
| Architecture docs | `docs/memory/architecture.md` | R | | Documented config |

## Process

### Step 1: Extract Env Vars from .env.example

Read `.env.example` and extract all variable names and their placeholder values.
Build a set: `documented_vars`

### Step 2: Extract Env Vars from docker-compose

Read `docker-compose.yml` and `docker-compose.prod.yml`. Extract:
- All `environment:` entries (both `KEY=value` and `KEY: value` forms)
- All `${VAR}` and `${VAR:-default}` references
- Which service uses which vars

Build a map: `var_name -> [services that use it]`

### Step 3: Extract Env Vars from Backend Code

Grep `src/backend/` for:
- `os.environ.get("VAR"` and `os.environ["VAR"]`
- `os.getenv("VAR"`
- `config.VAR` patterns where `config.py` reads from env

Read `src/backend/config.py` specifically and extract all env var references.

Build a set: `backend_vars`

### Step 4: Extract Env Vars from MCP Server

Grep `src/mcp-server/src/` for:
- `process.env.VAR`
- `process.env["VAR"]`

Build a set: `mcp_vars`

### Step 5: Extract Frontend Env Vars

Grep `src/frontend/` for:
- `import.meta.env.VITE_`

Build a set: `frontend_vars`

### Step 6: Cross-Reference

**6a. Used but undocumented:**
For each var in `backend_vars + mcp_vars + frontend_vars + docker_compose_vars`:
- Check if it exists in `.env.example`
- Flag missing ones (excluding well-known system vars like `PATH`, `HOME`, `NODE_ENV`)

**6b. Documented but unused:**
For each var in `.env.example`:
- Check if it appears in any code or docker-compose file
- Flag dead config entries

**6c. Docker-compose vs .env.example:**
For each `${VAR}` in docker-compose files:
- Check if `.env.example` provides a value
- Flag vars that docker-compose expects but .env.example doesn't define

**6d. Local vs production divergence:**
Compare `docker-compose.yml` and `docker-compose.prod.yml`:
- Services present in one but not the other (expected — document why)
- Env vars in one but not the other — flag as potential misconfiguration
- Port mappings that conflict

### Step 7: Check for Hardcoded Config

Grep `src/backend/` for patterns that should be env vars:
- Hardcoded port numbers (except well-known like 8000, 80, 443)
- Hardcoded hostnames (except `localhost`, `127.0.0.1`, service names from docker-compose)
- Hardcoded API URLs

Flag as informational — not all are violations, but worth reviewing.

### Step 8: Generate Report

Output a summary:

```
## Config Validation Report

### Env Var Coverage
| Var | .env.example | docker-compose | Backend | MCP | Frontend | Status |
|-----|-------------|----------------|---------|-----|----------|--------|
| ADMIN_PASSWORD | Y | Y | Y | - | - | OK |
| NEW_VAR | - | - | Y | - | - | UNDOCUMENTED |
| OLD_VAR | Y | - | - | - | - | UNUSED |
...

### Issues

#### Used but Undocumented (add to .env.example)
- `VAR_NAME` — used in `src/backend/config.py:42`

#### Documented but Unused (remove from .env.example)
- `OLD_VAR` — not referenced anywhere in code

#### Docker Compose Missing from .env.example
- `${VAR}` in docker-compose.yml service `backend` — no .env.example entry

#### Local vs Production Divergence
- `VAR` in docker-compose.yml but not docker-compose.prod.yml

#### Hardcoded Config (informational)
- `src/backend/services/foo.py:88` — hardcoded URL `http://some-service:3000`

**Result: X issues found (Y must-fix, Z informational)**
```

## Outputs

- Markdown report printed to conversation
- No files created or modified
