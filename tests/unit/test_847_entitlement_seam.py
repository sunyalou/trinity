"""Tests for #847 Phase 0 enterprise seam.

Verifies:

1. ``EntitlementService.is_entitled`` returns True for every feature
   in the default (stub) configuration.
2. ``TRINITY_OSS_ONLY=1`` flips every check to False — the
   ``oss_only`` deny path.
3. ``requires_entitlement(feature_id)`` raises HTTP 403 when the
   service denies and returns None on allow.
4. ``list_entitled_features()`` reports the known feature IDs in
   the OSS build.
5. ``_set_for_testing`` cleanly swaps the singleton.

The conditional ``register_enterprise`` import in ``main.py`` is
covered by an integration test (live container) rather than a unit
test — the unit path can't faithfully simulate the submodule's
absence.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


# -----------------------------------------------------------------------------
# EntitlementService default behaviour
# -----------------------------------------------------------------------------


def test_default_is_entitled_returns_true(monkeypatch):
    """Stub mode (no TRINITY_OSS_ONLY): every feature_id is entitled."""
    monkeypatch.delenv("TRINITY_OSS_ONLY", raising=False)
    from services.entitlement_service import EntitlementService

    svc = EntitlementService()
    assert svc.is_entitled("sso") is True
    assert svc.is_entitled("scim") is True
    assert svc.is_entitled("siem") is True
    # Unknown features also return True in stub mode — the seam doesn't
    # pretend to know the catalogue yet (that's Phase 1 license claims).
    assert svc.is_entitled("not-a-real-feature") is True


def test_default_list_entitled_features(monkeypatch):
    """Stub mode reports the known enterprise feature catalogue."""
    monkeypatch.delenv("TRINITY_OSS_ONLY", raising=False)
    from services.entitlement_service import EntitlementService

    svc = EntitlementService()
    features = svc.list_entitled_features()
    assert "sso" in features
    assert "scim" in features
    assert "siem" in features


# -----------------------------------------------------------------------------
# TRINITY_OSS_ONLY=1 deny path
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes"])
def test_oss_only_denies_every_feature(monkeypatch, value):
    """Any truthy spelling of TRINITY_OSS_ONLY flips all checks False."""
    monkeypatch.setenv("TRINITY_OSS_ONLY", value)
    # Reimport so the constructor re-reads the env var.
    if "services.entitlement_service" in sys.modules:
        del sys.modules["services.entitlement_service"]
    from services.entitlement_service import EntitlementService

    svc = EntitlementService()
    assert svc.is_entitled("sso") is False
    assert svc.is_entitled("scim") is False
    assert svc.list_entitled_features() == []


@pytest.mark.parametrize("value", ["0", "false", "no", ""])
def test_oss_only_falsy_keeps_entitlements(monkeypatch, value):
    """Falsy spellings (and empty string) leave the default stub
    behaviour intact."""
    monkeypatch.setenv("TRINITY_OSS_ONLY", value)
    if "services.entitlement_service" in sys.modules:
        del sys.modules["services.entitlement_service"]
    from services.entitlement_service import EntitlementService

    svc = EntitlementService()
    assert svc.is_entitled("sso") is True


# -----------------------------------------------------------------------------
# `requires_entitlement` dependency factory
# -----------------------------------------------------------------------------


def _import_requires_entitlement_or_skip():
    """Import ``dependencies.requires_entitlement`` or skip if backend
    venv isn't available locally (e.g. ``passlib`` missing in a stub
    dev environment). CI installs the full backend deps so this
    skip never fires there."""
    try:
        if "dependencies" in sys.modules:
            del sys.modules["dependencies"]
        from dependencies import requires_entitlement
        return requires_entitlement
    except ImportError as e:
        pytest.skip(f"backend venv required (no `dependencies` import: {e})")


def test_requires_entitlement_allows_when_entitled(monkeypatch):
    """The Depends() callable returns None on allow."""
    monkeypatch.delenv("TRINITY_OSS_ONLY", raising=False)
    if "services.entitlement_service" in sys.modules:
        del sys.modules["services.entitlement_service"]
    requires_entitlement = _import_requires_entitlement_or_skip()

    inner = requires_entitlement("sso")
    assert inner() is None


def test_requires_entitlement_raises_403_when_denied(monkeypatch):
    """Deny path raises HTTPException(403) with the feature_id in detail."""
    from fastapi import HTTPException

    monkeypatch.setenv("TRINITY_OSS_ONLY", "1")
    if "services.entitlement_service" in sys.modules:
        del sys.modules["services.entitlement_service"]
    requires_entitlement = _import_requires_entitlement_or_skip()

    inner = requires_entitlement("sso")
    with pytest.raises(HTTPException) as exc:
        inner()
    assert exc.value.status_code == 403
    assert "sso" in exc.value.detail


# -----------------------------------------------------------------------------
# _set_for_testing
# -----------------------------------------------------------------------------


def test_set_for_testing_swaps_singleton(monkeypatch):
    """Replacing the singleton lets tests force specific behaviour
    without monkeypatching the env or sys.modules."""
    monkeypatch.delenv("TRINITY_OSS_ONLY", raising=False)
    if "services.entitlement_service" in sys.modules:
        del sys.modules["services.entitlement_service"]
    import services.entitlement_service as ent_mod

    class _StubFalse:
        def is_entitled(self, _feature_id):
            return False

        def list_entitled_features(self):
            return []

    ent_mod._set_for_testing(_StubFalse())
    try:
        assert ent_mod.entitlement_service.is_entitled("sso") is False
        assert ent_mod.entitlement_service.list_entitled_features() == []
    finally:
        ent_mod._set_for_testing(None)  # restore default
    # Restored — stub-default singleton behaviour returns True again
    assert ent_mod.entitlement_service.is_entitled("sso") is True


# -----------------------------------------------------------------------------
# Static check: main.py must import enterprise conditionally
# -----------------------------------------------------------------------------


def test_main_py_uses_conditional_enterprise_import():
    """The enterprise loader must be in a try/except ImportError so
    OSS-only builds (no submodule) start cleanly."""
    src = (_BACKEND / "main.py").read_text(encoding="utf-8")
    # Find the import line
    idx = src.find("from enterprise import register_enterprise")
    assert idx != -1, "main.py must import register_enterprise from enterprise"
    # The preceding ~150 chars should contain `try:`
    window = src[max(0, idx - 200) : idx]
    assert "try:" in window, (
        "enterprise import must be inside a try/except ImportError block "
        "so OSS-only builds (no submodule) boot cleanly"
    )
    # And the trailing block should catch ImportError
    tail = src[idx : idx + 400]
    assert "except ImportError" in tail, (
        "enterprise import must be guarded by `except ImportError`"
    )
