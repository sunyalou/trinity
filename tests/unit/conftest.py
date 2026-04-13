"""
Unit test conftest — overrides the parent conftest's autouse fixtures.

These tests run without a backend connection (no Docker, no API).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

# Ensure src/backend is importable before test modules are collected.
#
# BACKLOG-001: tests/unit/test_backlog.py does `from models import ...`, which
# in turn does `from utils.helpers import ...`. pytest auto-adds `tests/` to
# sys.path at position 0, and `tests/utils/__init__.py` exists as a test
# helpers package. Just adding `src/backend` to sys.path isn't enough because
# pytest's entry stays at position 0 — the shadow `utils` wins first.
#
# Fix: load `src/backend/utils/__init__.py` directly via importlib and
# install it under the name `utils` in sys.modules BEFORE any test imports
# backend code. After that, `from utils.helpers import ...` and
# `from utils.api_client import ...` both hit the right place (backend's
# `utils` has `helpers.py`; tests' `utils` has `api_client.py`). Because
# we register under the module name `utils`, test helpers must be imported
# via `from utils import api_client` — the existing helpers use absolute
# `from utils.api_client import TrinityApiClient` which still resolves via
# sys.path lookup on attribute access. To avoid breakage we also install
# the backend's `utils` submodules explicitly and leave the test helpers
# alone (unit tests don't use them).
_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)


def _preload_backend_utils():
    """Install src/backend/utils as the canonical `utils` package for unit
    tests. Uses importlib's file-based loader so sys.path ordering can't
    shadow it later.
    """
    utils_init = _BACKEND / "utils" / "__init__.py"
    if not utils_init.exists():
        return
    spec = importlib.util.spec_from_file_location(
        "utils", str(utils_init), submodule_search_locations=[str(_BACKEND / "utils")]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["utils"] = module
    spec.loader.exec_module(module)
    # Preload helpers so `from utils.helpers import X` resolves without
    # triggering sys.path-based lookup.
    helpers_spec = importlib.util.spec_from_file_location(
        "utils.helpers", str(_BACKEND / "utils" / "helpers.py")
    )
    helpers_mod = importlib.util.module_from_spec(helpers_spec)
    sys.modules["utils.helpers"] = helpers_mod
    helpers_spec.loader.exec_module(helpers_mod)
    module.helpers = helpers_mod  # type: ignore[attr-defined]


# Evict any shadow `utils` that parent conftest already cached, then preload
# backend's utils package.
for _mod in list(sys.modules):
    if _mod == "utils" or _mod.startswith("utils."):
        sys.modules.pop(_mod, None)
_preload_backend_utils()


@pytest.fixture(autouse=True)
def cleanup_after_test():
    """Override parent's cleanup_after_test that requires api_client."""
    yield
