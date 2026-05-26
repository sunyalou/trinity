"""
Tests for the audit log dashboard distinct-value endpoints (#941).

Covers two layers:

* **DB layer**: ``PlatformAuditOperations.get_distinct_event_types`` /
  ``get_distinct_actor_types`` return sorted unique values; empty
  table returns ``[]``; NULL values excluded.
* **Router layer (static check)**: the two new ``GET /api/audit-log/
  distinct/*`` endpoints are admin-gated via ``Depends(require_admin)``
  and are registered BEFORE the ``/{event_id}`` catch-all route
  (Architectural Invariant #4).

The frontend route gate (``requiresEntitlement: 'audit'`` in
``router/index.js``) is a UI concern and covered by the Playwright
e2e suite, not here.
"""

from __future__ import annotations

import importlib.util
import inspect
import sqlite3
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_AUDIT_LOG_DDL = """
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    event_type TEXT NOT NULL,
    event_action TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    actor_email TEXT,
    actor_ip TEXT,
    mcp_key_id TEXT,
    mcp_key_name TEXT,
    mcp_scope TEXT,
    target_type TEXT,
    target_id TEXT,
    timestamp TEXT NOT NULL,
    details TEXT,
    request_id TEXT,
    source TEXT NOT NULL,
    endpoint TEXT,
    previous_hash TEXT,
    entry_hash TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


@pytest.fixture
def audit_ops(tmp_path, monkeypatch):
    """Build a tmp DB with a fresh audit_log table; return PlatformAuditOperations."""
    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_AUDIT_LOG_DDL)
    conn.commit()
    conn.close()

    # Force-reload db/connection.py so it picks up the new TRINITY_DB_PATH.
    sys.modules.pop("_audashb_db_connection", None)
    _load("_audashb_db_connection", _BACKEND / "db" / "connection.py")

    db_pkg = type(sys)("db")
    db_pkg.__path__ = [str(_BACKEND / "db")]
    monkeypatch.setitem(sys.modules, "db", db_pkg)
    monkeypatch.setitem(
        sys.modules, "db.connection", sys.modules["_audashb_db_connection"]
    )

    spec = importlib.util.spec_from_file_location(
        "db.audit", str(_BACKEND / "db" / "audit.py")
    )
    audit_mod = importlib.util.module_from_spec(spec)
    sys.modules["db.audit"] = audit_mod
    spec.loader.exec_module(audit_mod)
    return audit_mod.PlatformAuditOperations()


def _insert(
    db_path: Path,
    *,
    event_id: str,
    event_type: str = "test",
    actor_type: str = "system",
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO audit_log
            (event_id, event_type, event_action, actor_type,
             timestamp, source)
        VALUES (?, ?, ?, ?, datetime('now'), 'api')
        """,
        (event_id, event_type, "noop", actor_type),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# get_distinct_event_types
# ---------------------------------------------------------------------------


def test_distinct_event_types_empty_table_returns_empty_list(audit_ops):
    """Empty audit_log → []. No exceptions, no None."""
    result = audit_ops.get_distinct_event_types()
    assert result == []


def test_distinct_event_types_returns_sorted_unique_list(audit_ops, tmp_path):
    db_path = tmp_path / "trinity.db"

    # Seed in non-sorted order with duplicates.
    _insert(db_path, event_id="e1", event_type="agent_lifecycle")
    _insert(db_path, event_id="e2", event_type="authentication")
    _insert(db_path, event_id="e3", event_type="agent_lifecycle")  # dup
    _insert(db_path, event_id="e4", event_type="credentials")
    _insert(db_path, event_id="e5", event_type="authentication")  # dup

    result = audit_ops.get_distinct_event_types()

    assert result == [
        "agent_lifecycle",
        "authentication",
        "credentials",
    ]


# ---------------------------------------------------------------------------
# get_distinct_actor_types
# ---------------------------------------------------------------------------


def test_distinct_actor_types_empty_table_returns_empty_list(audit_ops):
    result = audit_ops.get_distinct_actor_types()
    assert result == []


def test_distinct_actor_types_returns_sorted_unique_list(audit_ops, tmp_path):
    db_path = tmp_path / "trinity.db"

    _insert(db_path, event_id="a1", actor_type="user")
    _insert(db_path, event_id="a2", actor_type="agent")
    _insert(db_path, event_id="a3", actor_type="mcp_client")
    _insert(db_path, event_id="a4", actor_type="user")  # dup
    _insert(db_path, event_id="a5", actor_type="system")

    result = audit_ops.get_distinct_actor_types()

    assert result == ["agent", "mcp_client", "system", "user"]


# ---------------------------------------------------------------------------
# Router-layer static check: admin gate + ordering against /{event_id}
# ---------------------------------------------------------------------------


def test_distinct_endpoints_admin_gated_and_before_catch_all():
    """Static check on `routers/audit_log.py`:

    1. Both `/distinct/event-types` and `/distinct/actor-types` are
       declared with `Depends(require_admin)` (matches the rest of the
       audit-log router).
    2. Their `@router.get` decorators appear BEFORE the `@router.get(
       "/{event_id}")` catch-all — Architectural Invariant #4 (static
       routes before parametrised). If a future refactor moves the
       distinct decorators below the catch-all, FastAPI will route
       `/distinct/event-types` to ``get_audit_log_entry`` with
       ``event_id="distinct"`` (silent 404 on the dashboard).
    """
    src = (_BACKEND / "routers" / "audit_log.py").read_text(encoding="utf-8")

    # Decorator order — invariant #4.
    idx_distinct_event = src.find('@router.get("/distinct/event-types"')
    idx_distinct_actor = src.find('@router.get("/distinct/actor-types"')
    idx_catch_all = src.find('@router.get("/{event_id}"')

    assert idx_distinct_event != -1, "distinct/event-types endpoint missing"
    assert idx_distinct_actor != -1, "distinct/actor-types endpoint missing"
    assert idx_catch_all != -1, "/{event_id} endpoint missing"
    assert idx_distinct_event < idx_catch_all, (
        "/distinct/event-types must be declared BEFORE /{event_id} "
        "(invariant #4); else FastAPI 404s the dashboard dropdown."
    )
    assert idx_distinct_actor < idx_catch_all, (
        "/distinct/actor-types must be declared BEFORE /{event_id} "
        "(invariant #4)"
    )

    # Admin gate — both endpoints must depend on require_admin.
    # Slice the source between each decorator and the next one (or
    # end-of-file) and assert require_admin appears in the handler body.
    next_after_event = min(
        i
        for i in [
            src.find("\n@", idx_distinct_event + 1),
            len(src),
        ]
        if i != -1 and i > idx_distinct_event
    )
    next_after_actor = min(
        i
        for i in [
            src.find("\n@", idx_distinct_actor + 1),
            len(src),
        ]
        if i != -1 and i > idx_distinct_actor
    )

    event_handler_src = src[idx_distinct_event:next_after_event]
    actor_handler_src = src[idx_distinct_actor:next_after_actor]

    assert "Depends(require_admin)" in event_handler_src, (
        "/distinct/event-types must be admin-gated (Depends(require_admin))"
    )
    assert "Depends(require_admin)" in actor_handler_src, (
        "/distinct/actor-types must be admin-gated (Depends(require_admin))"
    )


def test_distinct_endpoints_do_not_apply_entitlement_gate():
    """#941 premise 3 (revised): the audit-log endpoints — including the
    new distinct ones — stay OSS. Only the OSS-side dashboard ROUTE is
    entitlement-gated (in ``src/frontend/src/router/index.js``).

    This test guards against a future commit silently slapping
    ``requires_entitlement("audit")`` on the backend, which would be a
    breaking change for OSS admins who curl the audit endpoints.
    """
    src = (_BACKEND / "routers" / "audit_log.py").read_text(encoding="utf-8")
    assert "requires_entitlement" not in src, (
        "audit_log router stayed OSS by design (#941 premise 3); if you "
        "want to flip this to enterprise-gated, file an ADR — the change "
        "breaks every curl-using admin in OSS-only deploys."
    )
