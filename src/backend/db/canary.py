"""
Canary invariant violations database operations (CANARY-001 / Issue #411).

Append-mostly access to the `canary_violations` table populated by the
continuous orchestration-invariant harness. `services/canary_service.py`
writes one row per fired check each cycle; the read API surfaces them to
admins for triage.

`observed_state` is stored as a JSON string per invariant; the helpers
parse it on the way out so callers see a dict.

Converted from raw sqlite3 to SQLAlchemy Core for the configurable database
backend (#300) so it runs unchanged on both SQLite and PostgreSQL.
"""

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import select, insert, func, and_

from .engine import get_engine
from .tables import canary_violations


# Tier and severity values are validated at write time so the read API can
# expose them as plain strings without a DB-level CHECK constraint.
_VALID_TIERS = {"A", "B"}
_VALID_SEVERITIES = {"critical", "major", "minor"}


class CanaryOperations:
    """Database operations for the canary invariant violations table."""

    # ---------------------------------------------------------------------
    # Write
    # ---------------------------------------------------------------------

    def insert_violation(
        self,
        invariant_id: str,
        tier: str,
        severity: str,
        snapshot_time: str,
        observed_state: Dict[str, Any],
        signal_query: Optional[str] = None,
    ) -> int:
        """Insert a violation row, returning the new id.

        `observed_state` is JSON-serialized here so the caller passes a dict.
        """
        if tier not in _VALID_TIERS:
            raise ValueError(f"invalid tier {tier!r}; expected one of {_VALID_TIERS}")
        if severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"invalid severity {severity!r}; expected one of {_VALID_SEVERITIES}"
            )

        stmt = insert(canary_violations).values(
            invariant_id=invariant_id,
            tier=tier,
            severity=severity,
            snapshot_time=snapshot_time,
            observed_state=json.dumps(observed_state),
            signal_query=signal_query,
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return int(result.inserted_primary_key[0])

    # ---------------------------------------------------------------------
    # Read
    # ---------------------------------------------------------------------

    def list_violations(
        self,
        invariant_id: Optional[str] = None,
        severity: Optional[str] = None,
        tier: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Query violations with optional filters, newest first."""
        conditions = []

        if invariant_id:
            conditions.append(canary_violations.c.invariant_id == invariant_id)
        if severity:
            conditions.append(canary_violations.c.severity == severity)
        if tier:
            conditions.append(canary_violations.c.tier == tier)
        if start_time:
            conditions.append(canary_violations.c.snapshot_time >= start_time)
        if end_time:
            conditions.append(canary_violations.c.snapshot_time <= end_time)

        stmt = (
            select(canary_violations)
            .where(and_(*conditions))
            .order_by(
                canary_violations.c.snapshot_time.desc(),
                canary_violations.c.id.desc(),
            )
            .limit(int(limit))
            .offset(int(offset))
        )

        with get_engine().connect() as conn:
            return [
                self._row_to_dict(row)
                for row in conn.execute(stmt).mappings().all()
            ]

    def count_violations(
        self,
        invariant_id: Optional[str] = None,
        severity: Optional[str] = None,
        tier: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> int:
        """Return total count for a filter (independent of limit/offset)."""
        conditions = []

        if invariant_id:
            conditions.append(canary_violations.c.invariant_id == invariant_id)
        if severity:
            conditions.append(canary_violations.c.severity == severity)
        if tier:
            conditions.append(canary_violations.c.tier == tier)
        if start_time:
            conditions.append(canary_violations.c.snapshot_time >= start_time)
        if end_time:
            conditions.append(canary_violations.c.snapshot_time <= end_time)

        stmt = select(func.count()).select_from(canary_violations).where(
            and_(*conditions)
        )

        with get_engine().connect() as conn:
            return int(conn.execute(stmt).scalar_one())

    def get_violation(self, violation_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a single violation by primary key."""
        stmt = select(canary_violations).where(
            canary_violations.c.id == int(violation_id)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return self._row_to_dict(row) if row else None

    def get_latest_per_invariant(self) -> Dict[str, Dict[str, Any]]:
        """Return the most recent violation per invariant_id.

        Used by `CanaryService` for green→red transition detection: if the
        latest stored violation for an invariant predates the current
        snapshot, this cycle is a fresh transition that warrants a Slack
        webhook post.
        """
        latest = (
            select(
                canary_violations.c.invariant_id,
                func.max(canary_violations.c.id).label("max_id"),
            )
            .group_by(canary_violations.c.invariant_id)
            .subquery()
        )
        stmt = select(canary_violations).join(
            latest, canary_violations.c.id == latest.c.max_id
        )
        with get_engine().connect() as conn:
            return {
                row["invariant_id"]: self._row_to_dict(row)
                for row in conn.execute(stmt).mappings().all()
            }

    def stats_by_invariant(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Aggregate counts by invariant_id and severity for dashboard tiles."""
        conditions = []
        if start_time:
            conditions.append(canary_violations.c.snapshot_time >= start_time)
        if end_time:
            conditions.append(canary_violations.c.snapshot_time <= end_time)

        with get_engine().connect() as conn:
            total = int(
                conn.execute(
                    select(func.count())
                    .select_from(canary_violations)
                    .where(and_(*conditions))
                ).scalar_one()
            )

            by_invariant_rows = conn.execute(
                select(
                    canary_violations.c.invariant_id,
                    func.count().label("cnt"),
                )
                .where(and_(*conditions))
                .group_by(canary_violations.c.invariant_id)
                .order_by(func.count().desc())
            ).mappings().all()
            by_invariant = {
                row["invariant_id"]: int(row["cnt"]) for row in by_invariant_rows
            }

            by_severity_rows = conn.execute(
                select(
                    canary_violations.c.severity,
                    func.count().label("cnt"),
                )
                .where(and_(*conditions))
                .group_by(canary_violations.c.severity)
                .order_by(func.count().desc())
            ).mappings().all()
            by_severity = {
                row["severity"]: int(row["cnt"]) for row in by_severity_rows
            }

            return {
                "total": total,
                "by_invariant": by_invariant,
                "by_severity": by_severity,
            }

    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        """Convert a RowMapping to dict; parse `observed_state` JSON."""
        result = dict(row)
        observed = result.get("observed_state")
        if observed:
            try:
                result["observed_state"] = json.loads(observed)
            except (TypeError, ValueError):
                # Leave as raw string if not valid JSON.
                pass
        return result
