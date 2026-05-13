#!/usr/bin/env python3
"""
One-shot backfill: clean up orphan rows left behind by historical agent
deletions (Issue #816).

Pre-#816, `DELETE /api/agents/{name}` only touched ~10 of the ~40 tables
that reference an agent. Instances that ran any historical agent deletes
now carry orphan rows in the other ~30 tables. CANARY-001 L-03 surfaces
8 of these per ghost agent because its scope is *active* orchestration
state; this script scans the full registry.

Operates strictly on the CASCADE-policy tables in
`db.agent_cleanup.AGENT_REFS` — KEEP-policy rows (rolling-window billing
/ forever financial records) are never touched.

Usage:
    python scripts/management/backfill_agent_orphans.py            # dry-run
    python scripts/management/backfill_agent_orphans.py --apply    # delete rows
    python scripts/management/backfill_agent_orphans.py --apply --quiet

Environment:
    TRINITY_DB_PATH    Path to trinity.db. Defaults to ~/trinity-data/trinity.db
                       (matches the deploy script's bind mount).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path


def _load_cleanup_module():
    """Load db/agent_cleanup.py WITHOUT triggering db/__init__.py.

    db/__init__.py eagerly imports the entire domain layer (pydantic,
    fastapi, redis, …) which makes this script depend on a full backend
    venv. agent_cleanup.py is intentionally stdlib-only — load it by
    file path to keep the script runnable from any Python 3.10+.
    """
    import importlib.util

    cleanup_path = (
        Path(__file__).resolve().parent.parent.parent
        / "src" / "backend" / "db" / "agent_cleanup.py"
    )
    spec = importlib.util.spec_from_file_location(
        "agent_cleanup_standalone", cleanup_path
    )
    module = importlib.util.module_from_spec(spec)
    # dataclasses introspects cls.__module__ via sys.modules.get() — must
    # be registered before exec_module or @dataclass raises AttributeError
    # on the manually-loaded module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _db_path() -> str:
    env = os.environ.get("TRINITY_DB_PATH")
    if env:
        return env
    return str(Path.home() / "trinity-data" / "trinity.db")


def _open(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise SystemExit(f"DB not found at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip())
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete orphan rows. Default is dry-run.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-agent detail output.",
    )
    args = parser.parse_args()

    cleanup = _load_cleanup_module()
    cascade_delete = cleanup.cascade_delete
    find_orphan_agent_names = cleanup.find_orphan_agent_names

    db_path = _db_path()
    print(f"DB: {db_path}")
    print(f"Mode: {'APPLY' if args.apply else 'dry-run'}")
    print()

    conn = _open(db_path)
    try:
        orphans = find_orphan_agent_names(conn)
    except sqlite3.Error as exc:
        raise SystemExit(f"Orphan scan failed: {exc}")

    if not orphans:
        print("No orphan agent references found. Nothing to do.")
        return 0

    total_rows = sum(orphans.values())
    print(f"Found {len(orphans)} ghost agent name(s) with {total_rows} orphan row(s):")
    if not args.quiet:
        for name in sorted(orphans):
            print(f"  {name:40} {orphans[name]:>6} row(s)")
        print()

    if not args.apply:
        print("Dry-run only — no rows deleted. Re-run with --apply to clean up.")
        return 0

    # Apply: one agent at a time, each in its own transaction.
    cleaned_total = 0
    for name in sorted(orphans):
        try:
            deleted = cascade_delete(conn, name)
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            print(f"  FAILED {name}: {exc}", file=sys.stderr)
            continue
        cleaned = sum(deleted.values())
        cleaned_total += cleaned
        if not args.quiet:
            print(f"  {name:40} -> deleted {cleaned} row(s)")

    print()
    print(f"Done. Deleted {cleaned_total} row(s) across {len(orphans)} ghost agent(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
