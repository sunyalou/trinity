# Feature: Platform Audit Trail (SEC-001 / Issue #20)

## Overview
Cross-cutting append-only audit log for the Trinity platform. Records WHO did
WHAT across agent lifecycle, authentication, authorization, configuration,
credentials, MCP operations, git operations, and system events. Distinct from
the Process Engine's `audit_entries` table which is workflow-specific.

**Status**: Phase 1 implemented 2026-04-14. Phases 2–4 deferred to follow-up
PRs (issue #20 stays open until all phases ship).

| Phase | Scope | Status |
|---|---|---|
| **Phase 1** | Schema, immutability triggers, `PlatformAuditService`, `db/audit.py`, admin query API, unit tests | ✅ This PR |
| **Phase 2a** | Agent lifecycle integration (create / start / stop / delete) as working smoke test | ✅ This PR |
| Phase 2b | Remaining integrations (auth, sharing, settings, credentials, request_id middleware) | ⏳ Follow-up |
| Phase 3 | MCP server integration — TypeScript audit logging for MCP tool calls | ⏳ Follow-up |
| Phase 4 | Hash chain verification, CSV/JSON export, retention automation | ⏳ Follow-up |

## User Story
As a platform admin, I want a tamper-evident record of every administrative
action so that I can investigate incidents, respond to compliance requests,
and trace who modified what across the platform.

## Entry Points
- **Backend — query API** (admin-only): `src/backend/routers/audit_log.py` mounted at `/api/audit-log`
- **Backend — write API** (internal, used by Phase 2 integrations): `services/platform_audit_service.py` exports a global `platform_audit_service` instance with an `async log(...)` method
- **Database**: `audit_log` table in `~/trinity-data/trinity.db`

## Frontend Layer

None in Phase 1. Operators query via the OpenAPI docs at `/docs` or curl until
a Phase 4+ UI ships. The existing `/api/audit` router (which exposes Process
Engine audit) is unchanged.

## Backend Layer

### Database Schema (`db/schema.py`, `db/migrations.py`)

```sql
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,        -- UUID for deduplication
    event_type TEXT NOT NULL,             -- AuditEventType
    event_action TEXT NOT NULL,           -- "create" / "login_success" / etc.
    actor_type TEXT NOT NULL,             -- user / agent / mcp_client / system
    actor_id TEXT,                        -- user.id or agent_name or key_id
    actor_email TEXT,
    actor_ip TEXT,
    mcp_key_id TEXT,
    mcp_key_name TEXT,
    mcp_scope TEXT,
    target_type TEXT,                     -- agent / user / schedule / ...
    target_id TEXT,
    timestamp TEXT NOT NULL,              -- ISO 8601 UTC
    details TEXT,                         -- JSON payload
    request_id TEXT,                      -- correlation id
    source TEXT NOT NULL,                 -- api / mcp / scheduler / system
    endpoint TEXT,                        -- request path
    previous_hash TEXT,                   -- Phase 4
    entry_hash TEXT,                      -- Phase 4
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Indexes match the spec's documented query patterns
CREATE INDEX idx_audit_log_timestamp ON audit_log(timestamp DESC);
CREATE INDEX idx_audit_log_event_type ON audit_log(event_type, timestamp DESC);
CREATE INDEX idx_audit_log_actor ON audit_log(actor_type, actor_id, timestamp DESC);
CREATE INDEX idx_audit_log_target ON audit_log(target_type, target_id, timestamp DESC);
CREATE INDEX idx_audit_log_mcp_key ON audit_log(mcp_key_id, timestamp DESC);
CREATE INDEX idx_audit_log_request ON audit_log(request_id);

-- Append-only enforcement
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'Audit log entries cannot be modified'); END;

CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
WHEN OLD.timestamp > datetime('now', '-365 days')
BEGIN SELECT RAISE(ABORT, 'Audit log entries cannot be deleted within retention period'); END;
```

Migration `audit_log_table` (#31 in `db/migrations.py`) creates the table on
existing installs; fresh installs use `db/schema.py` directly.

### Database Operations (`db/audit.py`)

`PlatformAuditOperations` class with:
- `create_audit_entry(entry)` — insert
- `get_audit_entries(**filters, limit, offset)` — paginated list (newest first)
- `count_audit_entries(**filters)` — total for the same filter
- `get_audit_entry(event_id)` — single row by UUID
- `get_audit_entries_range(start_id, end_id)` — for hash-chain verification (Phase 4)
- `get_audit_stats(start_time, end_time)` — aggregate counts by event_type and actor_type

Composed into `DatabaseManager` as `db._audit_ops` with thin delegate methods
following the existing `db/permissions.py` → `database.py` pattern (invariant #2).

### Service Layer (`services/platform_audit_service.py`)

```python
from services.platform_audit_service import (
    platform_audit_service,
    AuditEventType,
)

await platform_audit_service.log(
    event_type=AuditEventType.AGENT_LIFECYCLE,
    event_action="create",
    source="api",
    actor_user=current_user,
    target_type="agent",
    target_id=agent_name,
    request_id=request.headers.get("X-Request-ID"),
    endpoint=str(request.url.path),
    details={"template": template_name},
)
```

The service:
- Resolves actor identity (user > agent > system > mcp_client) into the
  `actor_type` / `actor_id` / `actor_email` columns
- Generates a UUID `event_id` for deduplication
- JSON-encodes `details` before persisting
- Logs and swallows any DB error — **audit failures must never raise** so they
  cannot break the caller's primary operation
- Has dormant hash-chain code behind `_hash_chain_enabled = False` (Phase 4)

### API Endpoints (`routers/audit_log.py`)

Mounted at `/api/audit-log` to coexist with the existing `/api/audit` (Process
Engine audit). All endpoints require `Depends(require_admin)`.

| Method | Path | Description |
|---|---|---|
| GET | `/api/audit-log` | List audit entries with filters and pagination |
| GET | `/api/audit-log/stats` | Aggregate counts by event_type / actor_type |
| GET | `/api/audit-log/{event_id}` | Look up a single entry by UUID |

Filter parameters on the list endpoint: `event_type`, `actor_type`, `actor_id`,
`target_type`, `target_id`, `source`, `start_time`, `end_time`, `limit` (max 1000),
`offset`.

Route ordering: `/stats` is declared before `/{event_id}` in the router so it
isn't shadowed by the parameterized catch-all (invariant #4).

### Mounting (`main.py`)

```python
from routers.audit import router as audit_router            # Process Engine (existing)
from routers.audit_log import router as audit_log_router    # SEC-001 / #20

app.include_router(audit_router)       # /api/audit (Process Engine audit)
app.include_router(audit_log_router)   # /api/audit-log (Platform audit)
```

## Side Effects

- Inserts into `audit_log` are append-only — visible immediately to query API.
- No WebSocket events in Phase 1 (real-time audit feed is a Phase 4+ feature).
- Storage growth: ~500 bytes per entry. At 10K events/day that's ~180MB/year —
  manageable on a single SQLite file.
- Two read-only DB queries per `/api/audit-log` request (entries + count).

## Error Handling

| Failure | Behavior |
|---|---|
| DB write fails (disk full, locked, etc.) | `PlatformAuditService.log` catches, logs at ERROR with full traceback, returns `None`. **Caller is never affected.** |
| Duplicate `event_id` (UUID collision — vanishingly unlikely) | `IntegrityError` caught at the service layer; entry not written; logged. |
| UPDATE attempted on `audit_log` (anywhere in the codebase) | SQLite trigger raises `IntegrityError` with "Audit log entries cannot be modified". |
| DELETE within 365-day retention | Trigger raises with "Audit log entries cannot be deleted within retention period". |
| DELETE of >365-day entry | Allowed — supports future retention cleanup script. |
| Query returns no rows | `get_audit_entry` returns `None` → router returns 404; list endpoint returns `entries: []`. |
| Non-admin user calls any endpoint | `require_admin` raises 403 before the handler runs. |

**Invariant**: audit logging is best-effort metadata — building or writing it
can never fail the caller's primary operation. Phase 2 integration calls must
not propagate audit errors.

## Security Considerations

- **Admin-only access**: every endpoint depends on `require_admin`. Non-admin
  callers receive 403 with no information about whether the requested entry
  exists.
- **Append-only enforcement at the DB level**: SQLite triggers block UPDATE
  unconditionally and DELETE within the 365-day retention window. The triggers
  apply to direct SQL too — not just the ORM layer — so a future direct-DB
  attack still cannot tamper with history.
- **No PII in logs by design**: only metadata is stored — actor email, MCP key
  *name* (not value), user ID, agent name, endpoint path. No credential values,
  no token contents, no `.env` data, no chat message content.
- **Actor attribution always present**: `actor_type` is `NOT NULL` and the
  service falls back to `system`/`trinity-system` if no identifiable actor is
  passed, so every row has a clear "who".
- **Hash chain (Phase 4)**: dormant in Phase 1. When enabled via the toggle in
  `PlatformAuditService.__init__`, every entry's hash includes the previous
  entry's hash for tamper evidence. Verification endpoint planned for Phase 4.
- **Distinct from Process Engine audit**: the existing `/api/audit` (and its
  `audit_entries` table) is unchanged and continues to serve Process Engine
  workflow audit. The two systems coexist intentionally per the spec.

## Phase 2a — Agent Lifecycle Integration (shipped in this PR)

The first write-side integration lives in `routers/agents.py`. Four handlers
emit audit rows after a successful state transition:

| Handler | Event action | Details payload |
|---|---|---|
| `create_agent_endpoint` | `create` | `{template, base_image, agent_type}` |
| `start_agent_endpoint` | `start` | `{credentials_injection}` |
| `stop_agent_endpoint` | `stop` | `null` |
| `delete_agent_endpoint` | `delete` | `null` |

All four use the same kwargs shape:

```python
await platform_audit_service.log(
    event_type=AuditEventType.AGENT_LIFECYCLE,
    event_action="create",          # or start / stop / delete
    source="api",
    actor_user=current_user,
    actor_ip=request.client.host if request.client else None,
    target_type="agent",
    target_id=agent_name,
    endpoint=str(request.url.path),
    details={...},                  # action-specific
)
```

Log call lives in the **router**, not the service, so `current_user` stays in
scope without threading through service signatures. Placement is always
**after the state transition has committed** (container running, DB rows
deleted, etc.) — if the action fails partway, no audit row appears.

Audit failures are swallowed by the service layer and never fail the caller's
primary operation (verified by `test_service_never_raises_on_db_failure`).

## Out of Scope (Phase 2b–4 follow-ups)

- **Phase 2b** — remaining write integrations:
  - Authentication (`routers/auth.py` — login success/failure)
  - Authorization (`routers/sharing.py`, permission grant/revoke)
  - Settings changes (`routers/settings.py`)
  - Credential operations (`routers/credentials.py`)
  - Agent rename / recreate
  - Request-ID middleware for correlation
- **Phase 3** — MCP server integration (TypeScript) for tool-call audit
- **Phase 4** — hash chain verification, CSV/JSON export, retention automation, frontend admin UI, unified `/api/audit?system=platform|process`

## Testing

`tests/test_audit_log_unit.py` — 29 unit tests covering:

| Area | Tests |
|---|---|
| Insert / fetch | basic insert, JSON details serialization, unique event_id, missing-entry returns None |
| Append-only triggers | UPDATE blocked, DELETE within retention blocked, DELETE of >1yr-old entry allowed |
| Query filters | by event_type, actor_type, actor_id, target_type+target_id, time range |
| Ordering & pagination | newest-first, limit+offset, count independent of pagination |
| Range query | `get_audit_entries_range` for Phase 4 hash chain |
| Stats aggregation | total + by_event_type + by_actor_type |
| Service actor resolution | user, agent, mcp_client, system mcp_scope |
| Service serialization | JSON details encoding, request metadata (ip / request_id / endpoint) |
| Error contract | DB failure must return None and never raise; unique event_ids across rapid calls |
| Lifecycle integration | create / start / stop / delete produce correctly-shaped rows; full create→start→stop→delete flow produces 4 rows in temporal order |

Run: `.venv/bin/python -m pytest tests/test_audit_log_unit.py -v`

## Related Flows
- `docs/requirements/AUDIT_TRAIL_ARCHITECTURE.md` — full SEC-001 spec (840 lines, all four phases)
- `services/process_engine/services/audit.py` — the separate Process Engine audit service this coexists with
- `routers/audit.py` — existing `/api/audit` endpoint that exposes the Process Engine audit (unchanged)

## Architectural Compliance

- **Invariant #1** (Router → Service → DB): router holds only HTTP concerns, service holds only logic, db/audit.py holds only SQL
- **Invariant #2** (Class-per-domain DB ops with composition): `PlatformAuditOperations` composed into `DatabaseManager`
- **Invariant #3** (Schema in schema.py, migrations in migrations.py): both updated together
- **Invariant #4** (Router registration order): `/stats` before `/{event_id}` in router declaration
- **Invariant #8** (Auth pattern): every endpoint uses `Depends(require_admin)`
- **Invariant #15** (Pydantic models centralized): all response models in `routers/audit_log.py` (consistent with other routers that own their response shapes)
