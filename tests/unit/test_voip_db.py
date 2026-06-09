"""
Unit tests for the VoIP DB layer (VOIP-001, #1056).

Covers `db/voip.py` (binding CRUD + AuthToken encryption, call-log lifecycle,
durable daily-cap count) and the `_migrate_voip_tables` migration.

Module: src/backend/db/voip.py, src/backend/db/migrations.py
"""

import json
import os
import secrets
import sqlite3
import sys
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend, engine_conn  # noqa: E402


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    yield


@pytest.fixture
def voip_ops(db_backend):
    """VoipOperations + an engine-backed conn shim, on the active backend
    (db_harness, #300). Runs on SQLite and, when TEST_POSTGRES_URL is set,
    PostgreSQL. db_backend builds the full schema (incl. voip_bindings /
    voip_call_logs); the yielded shim mimics a sqlite3 connection for the
    tests' direct verification reads + legacy-row seeding."""
    from db import voip as voip_db

    yield voip_db.VoipOperations(), engine_conn()


# ---------------------------------------------------------------------------
# Binding CRUD + encryption
# ---------------------------------------------------------------------------

class TestBinding:
    def test_create_then_decrypt_round_trip(self, voip_ops):
        ops, _ = voip_ops
        token = "FAKE-TWILIO-VOICE-AUTH-TOKEN-not-real"
        created = ops.create_binding(
            agent_name="agent-voip-1",
            account_sid="AC" + "0" * 32,
            auth_token=token,
            from_number="+14155550100",
        )
        assert created["agent_name"] == "agent-voip-1"
        assert created["from_number"] == "+14155550100"
        assert created["daily_call_cap"] == 50  # default
        assert ops.get_decrypted_auth_token("agent-voip-1") == token

    def test_raw_token_is_encrypted_envelope(self, voip_ops):
        ops, conn = voip_ops
        token = "FAKE-TWILIO-VOICE-AUTH-TOKEN-not-real"
        ops.create_binding(
            agent_name="agent-voip-2",
            account_sid="AC" + "1" * 32,
            auth_token=token,
            from_number="+14155550101",
        )
        raw = conn.execute(
            "SELECT auth_token_encrypted FROM voip_bindings WHERE agent_name=?",
            ("agent-voip-2",),
        ).fetchone()[0]
        assert token not in raw
        env = json.loads(raw)
        assert env["algorithm"] == "AES-256-GCM"
        assert "ciphertext" in env and "nonce" in env

    def test_account_sid_stays_plaintext(self, voip_ops):
        ops, conn = voip_ops
        sid = "AC" + "2" * 32
        ops.create_binding(
            agent_name="agent-voip-sid", account_sid=sid,
            auth_token="t", from_number="+14155550102",
        )
        raw_sid = conn.execute(
            "SELECT account_sid FROM voip_bindings WHERE agent_name=?",
            ("agent-voip-sid",),
        ).fetchone()[0]
        assert raw_sid == sid

    def test_webhook_secret_is_unique_per_binding(self, voip_ops):
        ops, _ = voip_ops
        a = ops.create_binding(agent_name="a", account_sid="AC"+"3"*32,
                               auth_token="t", from_number="+14155550103")
        b = ops.create_binding(agent_name="b", account_sid="AC"+"4"*32,
                               auth_token="t", from_number="+14155550104")
        assert a["webhook_secret"] and b["webhook_secret"]
        assert a["webhook_secret"] != b["webhook_secret"]

    def test_custom_daily_cap_persisted(self, voip_ops):
        ops, _ = voip_ops
        b = ops.create_binding(agent_name="agent-cap", account_sid="AC"+"5"*32,
                               auth_token="t", from_number="+14155550105",
                               daily_call_cap=7)
        assert b["daily_call_cap"] == 7

    def test_delete_binding(self, voip_ops):
        ops, _ = voip_ops
        ops.create_binding(agent_name="agent-del", account_sid="AC"+"6"*32,
                           auth_token="t", from_number="+14155550106")
        assert ops.delete_binding("agent-del") is True
        assert ops.get_binding_by_agent("agent-del") is None
        assert ops.delete_binding("agent-del") is False  # idempotent


# ---------------------------------------------------------------------------
# Call logs + durable daily-cap count
# ---------------------------------------------------------------------------

class TestCallLogs:
    def test_create_and_count_within_window(self, voip_ops):
        ops, _ = voip_ops
        for i in range(3):
            ops.create_call_log(
                call_id=f"voip_call_{i}", agent_name="agent-x",
                to_number="+14155550199",
            )
        assert ops.count_calls_since("agent-x", hours=24) == 3
        assert ops.count_calls_since("other-agent", hours=24) == 0

    def test_old_calls_excluded_from_window(self, voip_ops):
        ops, conn = voip_ops
        # One recent (via API) + one ancient (manual, 2 days ago)
        ops.create_call_log(call_id="voip_recent", agent_name="agent-y",
                            to_number="+14155550200")
        conn.execute(
            "INSERT INTO voip_call_logs (call_id, agent_name, to_number, direction, status, started_at) "
            "VALUES (?, ?, ?, 'outbound', 'completed', ?)",
            ("voip_ancient", "agent-y", "+14155550200", "2020-01-01T00:00:00Z"),
        )
        conn.commit()
        assert ops.count_calls_since("agent-y", hours=24) == 1

    def test_update_call_status_lifecycle(self, voip_ops):
        ops, conn = voip_ops
        ops.create_call_log(call_id="voip_life", agent_name="agent-z",
                            to_number="+14155550201")
        ops.update_call_status("voip_life", "connected", twilio_call_sid="CA123")
        row = conn.execute(
            "SELECT status, twilio_call_sid, connected_at FROM voip_call_logs WHERE call_id=?",
            ("voip_life",),
        ).fetchone()
        assert row[0] == "connected" and row[1] == "CA123" and row[2] is not None

        ops.update_call_status("voip_life", "completed", duration_ms=42000)
        row = conn.execute(
            "SELECT status, ended_at, duration_ms FROM voip_call_logs WHERE call_id=?",
            ("voip_life",),
        ).fetchone()
        assert row[0] == "completed" and row[1] is not None and row[2] == 42000


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

class TestMigration:
    def test_migration_creates_tables_idempotently(self):
        from db.migrations import _migrate_voip_tables
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()

        _migrate_voip_tables(cur, conn)
        # Idempotent re-run must not raise (CREATE TABLE IF NOT EXISTS).
        _migrate_voip_tables(cur, conn)

        tables = {
            r[0] for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "voip_bindings" in tables
        assert "voip_call_logs" in tables

        cols = {r[1] for r in cur.execute("PRAGMA table_info(voip_bindings)").fetchall()}
        # inbound_number shipped up-front for Phase 2 (additive-only)
        assert {"agent_name", "auth_token_encrypted", "from_number",
                "inbound_number", "webhook_secret", "daily_call_cap"} <= cols
        conn.close()
