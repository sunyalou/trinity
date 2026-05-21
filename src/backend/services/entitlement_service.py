"""Entitlement Service — gates enterprise features at runtime (#847 seam).

Phase 0 stub. Answers ``is_entitled(feature_id)`` for the
``requires_entitlement`` FastAPI dependency in ``dependencies.py``.

This stub returns True for every feature_id. The seam exists so
enterprise routers can be wired into the app today (via the private
submodule at ``src/backend/enterprise/``) without each call site needing
a license-check conditional bolted on later. When the real license
mechanism lands (Phase 1 — Ed25519-signed token + offline verify), this
class gets the verification logic and every gated endpoint picks it up
with zero diff at the call site.

Why a class and not a module function:

* Phase 1 needs cached state (license blob, parsed claims, expiry,
  grace window). A class is the natural home.
* Tests can swap the singleton via ``_set_for_testing(service)`` without
  touching ``sys.modules``.

Public API
----------
* ``entitlement_service`` — module-level singleton, imported by the
  ``requires_entitlement`` dependency
* ``EntitlementService.is_entitled(feature_id: str) -> bool``
* ``EntitlementService.list_entitled_features() -> list[str]``

Read by:
* ``dependencies.requires_entitlement(...)`` — per-request gate
* ``routers/settings.get_feature_flags`` — UI hide/show signal
* (future) MCP server via ``GET /api/internal/entitlements``
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class EntitlementService:
    """In-process entitlement check.

    Phase 0 (this PR): all features entitled. The ``oss_only`` mode
    (env ``TRINITY_OSS_ONLY=1``) flips every check to False, useful for
    operators who want to lock down a Trinity instance to the OSS
    surface even when the enterprise submodule is mounted (e.g. for
    a clean compliance posture in a free-tier deployment).
    """

    def __init__(self) -> None:
        # When TRINITY_OSS_ONLY=1, deny every feature. Use cases:
        # operators running the public repo without enterprise, who
        # still want UI affordances to hide enterprise tabs; CI builds
        # that exercise the deny path.
        self._oss_only = os.getenv("TRINITY_OSS_ONLY", "0").lower() in {"1", "true", "yes"}
        if self._oss_only:
            logger.info(
                "[EntitlementService] TRINITY_OSS_ONLY=1 — all enterprise "
                "features will report as not-entitled"
            )

    def is_entitled(self, feature_id: str) -> bool:
        """Return True if the named feature is licensed for this instance.

        Phase 0: True for any feature_id unless OSS-only mode is set.
        Phase 1: cross-checks the license claim set.
        """
        if self._oss_only:
            return False
        # Phase 1 will replace this with a real license check.
        return True

    def list_entitled_features(self) -> list[str]:
        """Return the set of feature IDs this instance is entitled to.

        Used by ``GET /api/settings/feature-flags`` to drive UI tabs.
        Phase 0: returns the well-known feature IDs when entitled.
        """
        if self._oss_only:
            return []
        # Phase 0: the set known to the seam. Real implementations
        # would derive from license claims.
        return ["sso", "scim", "siem"]


# Module-level singleton — what `dependencies.requires_entitlement` calls.
entitlement_service = EntitlementService()


def _set_for_testing(service: Optional[EntitlementService]) -> None:
    """Replace the singleton. Test-only.

    Pass an instance with custom behaviour (e.g. ``is_entitled`` mocked
    to return False) to exercise the deny path without monkeypatching
    ``sys.modules`` or environment variables.

    Pass None to restore the default singleton.
    """
    global entitlement_service
    if service is None:
        entitlement_service = EntitlementService()
    else:
        entitlement_service = service
