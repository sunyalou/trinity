"""
Behavior tests for `db.agent_cleanup.cascade_delete` /
`cascade_rename` / `find_orphan_agent_names` (Issue #816).

Uses an ephemeral SQLite DB with a minimal subset of the schema —
enough to exercise every CASCADE / KEEP policy branch, plus the
link-chained tables (`public_chat_messages` via
`public_chat_sessions` via `agent_public_links`), plus the
multi-column tables (`agent_permissions` source/target).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _make_db(tmp_path) -> str:
    """Create an SQLite DB with a representative subset of Trinity's tables."""
    db_path = tmp_path / "trinity.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    # Parent
    cur.execute("CREATE TABLE agent_ownership (agent_name TEXT PRIMARY KEY)")

    # CASCADE: single-column agent_name
    for t in (
        "agent_sharing", "agent_schedules", "chat_sessions", "chat_messages",
        "agent_sessions", "agent_session_messages", "agent_activities",
        "agent_notifications", "agent_shared_folder_config", "agent_shared_files",
        "public_user_memory", "agent_git_config", "agent_sync_state",
        "agent_skills", "agent_tags", "agent_health_checks",
        "monitoring_alert_cooldowns", "agent_dashboard_values",
        "agent_dashboard_cache", "slack_channel_agents", "slack_active_threads",
        "telegram_bindings", "whatsapp_bindings", "nevermined_agent_config",
        "subscription_rate_limit_events", "operator_queue", "access_requests",
    ):
        cur.execute(
            f"CREATE TABLE {t} (id INTEGER PRIMARY KEY AUTOINCREMENT, agent_name TEXT)"
        )

    # KEEP
    cur.execute(
        "CREATE TABLE schedule_executions (id INTEGER PRIMARY KEY, agent_name TEXT)"
    )
    cur.execute(
        "CREATE TABLE nevermined_payment_log (id INTEGER PRIMARY KEY, agent_name TEXT)"
    )

    # mcp_api_keys with scope filter
    cur.execute(
        "CREATE TABLE mcp_api_keys (id INTEGER PRIMARY KEY, agent_name TEXT, scope TEXT)"
    )

    # Multi-column tables
    cur.execute(
        "CREATE TABLE agent_permissions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, source_agent TEXT, target_agent TEXT)"
    )
    cur.execute(
        "CREATE TABLE agent_event_subscriptions "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, subscriber_agent TEXT, source_agent TEXT)"
    )
    cur.execute(
        "CREATE TABLE agent_events "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, source_agent TEXT)"
    )

    # Public links + chained
    cur.execute(
        "CREATE TABLE agent_public_links "
        "(id TEXT PRIMARY KEY, agent_name TEXT)"
    )
    cur.execute(
        "CREATE TABLE public_link_usage "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, link_id TEXT)"
    )
    cur.execute(
        "CREATE TABLE public_link_verifications "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, link_id TEXT)"
    )
    cur.execute(
        "CREATE TABLE public_chat_sessions "
        "(id TEXT PRIMARY KEY, link_id TEXT)"
    )
    cur.execute(
        "CREATE TABLE public_chat_messages "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT)"
    )
    cur.execute(
        "CREATE TABLE slack_link_connections "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, link_id TEXT)"
    )

    # Channel bindings chained
    cur.execute(
        "CREATE TABLE telegram_chat_links "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, binding_id INTEGER)"
    )
    cur.execute(
        "CREATE TABLE telegram_group_configs "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, binding_id INTEGER)"
    )
    cur.execute(
        "CREATE TABLE whatsapp_chat_links "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, binding_id INTEGER)"
    )

    conn.commit()
    conn.close()
    return str(db_path)


def _seed_agent(conn, name: str, *, link_id: str = "lnk", binding_id: int = 1, session_id: str = "sess"):
    cur = conn.cursor()
    cur.execute("INSERT INTO agent_ownership(agent_name) VALUES (?)", (name,))

    # Single-column CASCADE: one row per table
    for t in (
        "agent_sharing", "agent_schedules", "chat_sessions", "chat_messages",
        "agent_sessions", "agent_session_messages", "agent_activities",
        "agent_notifications", "agent_shared_folder_config", "agent_shared_files",
        "public_user_memory", "agent_git_config", "agent_sync_state",
        "agent_skills", "agent_tags", "agent_health_checks",
        "monitoring_alert_cooldowns", "agent_dashboard_values",
        "agent_dashboard_cache", "slack_channel_agents", "slack_active_threads",
        "nevermined_agent_config", "subscription_rate_limit_events",
        "operator_queue", "access_requests",
    ):
        cur.execute(f"INSERT INTO {t}(agent_name) VALUES (?)", (name,))

    # Channel binding parents (need IDs for chained inserts)
    cur.execute("INSERT INTO telegram_bindings(id, agent_name) VALUES (?, ?)", (binding_id, name))
    cur.execute("INSERT INTO whatsapp_bindings(id, agent_name) VALUES (?, ?)", (binding_id, name))
    cur.execute("INSERT INTO telegram_chat_links(binding_id) VALUES (?)", (binding_id,))
    cur.execute("INSERT INTO telegram_group_configs(binding_id) VALUES (?)", (binding_id,))
    cur.execute("INSERT INTO whatsapp_chat_links(binding_id) VALUES (?)", (binding_id,))

    # KEEP
    cur.execute("INSERT INTO schedule_executions(agent_name) VALUES (?)", (name,))
    cur.execute("INSERT INTO nevermined_payment_log(agent_name) VALUES (?)", (name,))

    # MCP keys: one agent-scoped (should be deleted), one user-scoped (should NOT)
    cur.execute("INSERT INTO mcp_api_keys(agent_name, scope) VALUES (?, 'agent')", (name,))
    cur.execute("INSERT INTO mcp_api_keys(agent_name, scope) VALUES (?, 'user')", (name,))

    # Multi-column
    cur.execute(
        "INSERT INTO agent_permissions(source_agent, target_agent) VALUES (?, 'other')",
        (name,),
    )
    cur.execute(
        "INSERT INTO agent_permissions(source_agent, target_agent) VALUES ('other', ?)",
        (name,),
    )
    cur.execute(
        "INSERT INTO agent_event_subscriptions(subscriber_agent, source_agent) "
        "VALUES (?, 'other')",
        (name,),
    )
    cur.execute(
        "INSERT INTO agent_event_subscriptions(subscriber_agent, source_agent) "
        "VALUES ('other', ?)",
        (name,),
    )
    cur.execute("INSERT INTO agent_events(source_agent) VALUES (?)", (name,))

    # Public links + chained
    cur.execute(
        "INSERT INTO agent_public_links(id, agent_name) VALUES (?, ?)",
        (link_id, name),
    )
    cur.execute("INSERT INTO public_link_usage(link_id) VALUES (?)", (link_id,))
    cur.execute("INSERT INTO public_link_verifications(link_id) VALUES (?)", (link_id,))
    cur.execute(
        "INSERT INTO public_chat_sessions(id, link_id) VALUES (?, ?)",
        (session_id, link_id),
    )
    cur.execute("INSERT INTO public_chat_messages(session_id) VALUES (?)", (session_id,))
    cur.execute("INSERT INTO slack_link_connections(link_id) VALUES (?)", (link_id,))

    conn.commit()


def _count(conn, table: str, where: str = "1=1", params: tuple = ()) -> int:
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params)
    return cur.fetchone()[0]


# -----------------------------------------------------------------------------
# cascade_delete
# -----------------------------------------------------------------------------

def test_cascade_delete_removes_cascade_tables(tmp_path):
    """Every CASCADE-policy table loses the agent's rows."""
    from db.agent_cleanup import cascade_delete

    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    _seed_agent(conn, "doomed")

    cascade_delete(conn, "doomed")
    conn.commit()

    for t in (
        "agent_sharing", "agent_schedules", "chat_sessions", "chat_messages",
        "agent_activities", "agent_notifications", "agent_shared_folder_config",
        "agent_skills", "agent_tags", "agent_health_checks",
        "operator_queue", "access_requests", "telegram_bindings",
        "whatsapp_bindings", "nevermined_agent_config",
    ):
        assert _count(conn, t) == 0, f"{t} should be empty after cascade_delete"


def test_cascade_delete_preserves_keep_tables(tmp_path):
    """schedule_executions + nevermined_payment_log survive (billing / forever)."""
    from db.agent_cleanup import cascade_delete

    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    _seed_agent(conn, "doomed")

    cascade_delete(conn, "doomed")
    conn.commit()

    assert _count(conn, "schedule_executions") == 1
    assert _count(conn, "nevermined_payment_log") == 1


def test_cascade_delete_handles_multi_column_tables(tmp_path):
    """agent_permissions and agent_event_subscriptions are cleaned for
    rows where the agent appears in EITHER column."""
    from db.agent_cleanup import cascade_delete

    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    _seed_agent(conn, "doomed")

    cascade_delete(conn, "doomed")
    conn.commit()

    # Both rows (source AND target) should be gone
    assert _count(conn, "agent_permissions") == 0
    assert _count(conn, "agent_event_subscriptions") == 0
    assert _count(conn, "agent_events") == 0


def test_cascade_delete_mcp_keys_respects_scope_filter(tmp_path):
    """Only agent-scoped MCP keys are deleted — user-scoped keys survive."""
    from db.agent_cleanup import cascade_delete

    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    _seed_agent(conn, "doomed")

    cascade_delete(conn, "doomed")
    conn.commit()

    assert _count(conn, "mcp_api_keys", "scope = 'agent'") == 0
    assert _count(conn, "mcp_api_keys", "scope = 'user'") == 1


def test_cascade_delete_link_chained_tables(tmp_path):
    """public_link_usage / public_chat_messages / etc. are deleted via JOIN."""
    from db.agent_cleanup import cascade_delete

    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    _seed_agent(conn, "doomed")

    cascade_delete(conn, "doomed")
    conn.commit()

    for t in (
        "public_link_usage", "public_link_verifications",
        "public_chat_sessions", "public_chat_messages",
        "slack_link_connections", "telegram_chat_links",
        "telegram_group_configs", "whatsapp_chat_links",
        "agent_public_links",
    ):
        assert _count(conn, t) == 0, f"{t} should be empty after cascade_delete"


def test_cascade_delete_does_not_touch_other_agents(tmp_path):
    """Deleting agent A leaves agent B untouched."""
    from db.agent_cleanup import cascade_delete

    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    _seed_agent(conn, "alpha", link_id="lnk_a", binding_id=1, session_id="sess_a")
    _seed_agent(conn, "beta",  link_id="lnk_b", binding_id=2, session_id="sess_b")

    cascade_delete(conn, "alpha")
    conn.commit()

    # alpha's rows gone
    assert _count(conn, "agent_sharing", "agent_name = ?", ("alpha",)) == 0
    assert _count(conn, "agent_public_links", "id = ?", ("lnk_a",)) == 0
    assert _count(conn, "telegram_chat_links", "binding_id = ?", (1,)) == 0

    # beta's rows untouched
    assert _count(conn, "agent_sharing", "agent_name = ?", ("beta",)) == 1
    assert _count(conn, "agent_public_links", "id = ?", ("lnk_b",)) == 1
    assert _count(conn, "telegram_chat_links", "binding_id = ?", (2,)) == 1


def test_cascade_delete_idempotent(tmp_path):
    """Running cascade_delete twice is a no-op the second time."""
    from db.agent_cleanup import cascade_delete

    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    _seed_agent(conn, "doomed")

    cascade_delete(conn, "doomed")
    second = cascade_delete(conn, "doomed")
    conn.commit()

    assert second == {}, f"Second call should report no deletes: {second}"


def test_cascade_delete_returns_row_counts(tmp_path):
    """Return value reflects what was deleted."""
    from db.agent_cleanup import cascade_delete

    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    _seed_agent(conn, "doomed")

    deleted = cascade_delete(conn, "doomed")
    conn.commit()

    assert deleted.get("agent_sharing") == 1
    # Multi-column tables show up with :column suffix
    assert deleted.get("agent_permissions:source_agent") == 1
    assert deleted.get("agent_permissions:target_agent") == 1


# -----------------------------------------------------------------------------
# cascade_rename
# -----------------------------------------------------------------------------

def test_cascade_rename_touches_cascade_and_keep_alike(tmp_path):
    """Rename must touch every reference regardless of delete policy
    (KEEP tables still have rows referencing the OLD name)."""
    from db.agent_cleanup import cascade_rename

    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    _seed_agent(conn, "old")

    cascade_rename(conn, "old", "new")
    conn.commit()

    # CASCADE table
    assert _count(conn, "agent_sharing", "agent_name = ?", ("new",)) == 1
    # KEEP table
    assert _count(conn, "schedule_executions", "agent_name = ?", ("new",)) == 1
    assert _count(conn, "nevermined_payment_log", "agent_name = ?", ("new",)) == 1
    # Multi-column
    assert _count(conn, "agent_permissions", "source_agent = ?", ("new",)) == 1
    assert _count(conn, "agent_permissions", "target_agent = ?", ("new",)) == 1
    # MCP scope filter still respected on rename
    assert _count(conn, "mcp_api_keys", "scope = 'agent' AND agent_name = 'new'") == 1
    assert _count(conn, "mcp_api_keys", "scope = 'user' AND agent_name = 'old'") == 1


def test_cascade_rename_does_not_touch_other_agents(tmp_path):
    from db.agent_cleanup import cascade_rename

    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    _seed_agent(conn, "alpha", link_id="lnk_a", binding_id=1, session_id="sa")
    _seed_agent(conn, "beta",  link_id="lnk_b", binding_id=2, session_id="sb")

    cascade_rename(conn, "alpha", "alpha2")
    conn.commit()

    assert _count(conn, "agent_sharing", "agent_name = ?", ("alpha2",)) == 1
    assert _count(conn, "agent_sharing", "agent_name = ?", ("beta",)) == 1
    assert _count(conn, "agent_sharing", "agent_name = ?", ("alpha",)) == 0


# -----------------------------------------------------------------------------
# find_orphan_agent_names
# -----------------------------------------------------------------------------

def test_find_orphan_agent_names_finds_drift(tmp_path):
    """Orphan rows surface in find_orphan_agent_names with the right counts."""
    from db.agent_cleanup import find_orphan_agent_names

    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)

    # Live agent
    conn.cursor().execute("INSERT INTO agent_ownership(agent_name) VALUES ('live')")
    # Orphan rows (no agent_ownership entry for 'ghost')
    conn.cursor().execute("INSERT INTO agent_sharing(agent_name) VALUES ('ghost')")
    conn.cursor().execute("INSERT INTO agent_activities(agent_name) VALUES ('ghost')")
    conn.cursor().execute("INSERT INTO agent_activities(agent_name) VALUES ('ghost')")
    conn.cursor().execute("INSERT INTO agent_health_checks(agent_name) VALUES ('phantom')")
    conn.commit()

    orphans = find_orphan_agent_names(conn)

    assert orphans.get("ghost") == 3   # 1 sharing + 2 activities
    assert orphans.get("phantom") == 1
    assert "live" not in orphans


def test_find_orphan_agent_names_empty_when_no_agents(tmp_path):
    """Safety: refuse to operate when no agents exist (fresh-install
    DB would otherwise report every row as orphan)."""
    from db.agent_cleanup import find_orphan_agent_names

    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.cursor().execute("INSERT INTO agent_sharing(agent_name) VALUES ('x')")
    conn.commit()

    orphans = find_orphan_agent_names(conn)
    assert orphans == {}


def test_find_orphan_agent_names_ignores_keep_tables(tmp_path):
    """KEEP-policy rows aren't classified as orphan (they're allowed
    to outlive the agent)."""
    from db.agent_cleanup import find_orphan_agent_names

    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.cursor().execute("INSERT INTO agent_ownership(agent_name) VALUES ('live')")
    conn.cursor().execute("INSERT INTO schedule_executions(agent_name) VALUES ('billing-ghost')")
    conn.cursor().execute("INSERT INTO nevermined_payment_log(agent_name) VALUES ('billing-ghost')")
    conn.commit()

    orphans = find_orphan_agent_names(conn)
    assert "billing-ghost" not in orphans
