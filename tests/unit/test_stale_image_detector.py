"""Regression tests for #557: stale-image detection in start.sh.

The detector compares each platform image's ``.Created`` timestamp
against the latest mtime of its tracked dep-bearing files. The function
under test is ``scripts/deploy/_check_stale_images.py``'s ``main()`` —
covered here by mocking the docker subprocess and the filesystem mtime
on a tmp project root.
"""
from __future__ import annotations

import datetime
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "deploy" / "_check_stale_images.py"


@pytest.fixture
def detector(monkeypatch, tmp_path):
    """Load the detector module with PROJECT_ROOT pointed at a clean tmp tree."""
    spec = importlib.util.spec_from_file_location("stale_image_detector", str(_SCRIPT))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)

    # Build the tracked file tree under tmp_path so file mtimes are real.
    for _, file_paths in module.SERVICES.values():
        for rel in file_paths:
            target = tmp_path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("placeholder")
    return module, tmp_path


def _set_mtime(path: Path, when: datetime.datetime) -> None:
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def _mock_docker_inspect(timestamps: dict[str, str | None]):
    """Patch ``subprocess.check_output`` to return a stub creation time per image.

    ``timestamps`` maps image:tag → RFC3339 string (or ``None`` to simulate
    a missing image — raises CalledProcessError).
    """
    def fake(cmd, *args, **kwargs):
        # cmd is ['docker', 'image', 'inspect', '<image>', '--format', '{{.Created}}']
        image = cmd[3]
        ts = timestamps.get(image)
        if ts is None:
            raise subprocess.CalledProcessError(1, cmd)
        return ts
    return patch.object(subprocess, "check_output", side_effect=fake)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStaleImageDetector:
    """Detector returns service names exactly when their tracked files are
    newer than the image creation time."""

    def test_all_fresh_emits_nothing(self, detector, capsys):
        """No service should be flagged when every tracked file is older
        than its image."""
        module, root = detector
        # Files were created just now; pin them in the past.
        old = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        for _, file_paths in module.SERVICES.values():
            for rel in file_paths:
                _set_mtime(root / rel, old)

        # Image creation time AFTER all file mtimes.
        new = datetime.datetime(2026, 4, 1, tzinfo=datetime.timezone.utc)
        timestamps = {
            image: new.isoformat()
            for image, _ in module.SERVICES.values()
        }
        with _mock_docker_inspect(timestamps):
            module.main()

        captured = capsys.readouterr()
        assert captured.out.strip() == "", (
            f"Detector flagged services with no stale files: {captured.out!r}"
        )

    def test_dockerfile_newer_than_image_flags_service(self, detector, capsys):
        """The exact #557 scenario: Dockerfile was modified after the image
        was built, so the service must be flagged for rebuild."""
        module, root = detector
        old = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        for _, file_paths in module.SERVICES.values():
            for rel in file_paths:
                _set_mtime(root / rel, old)

        # Push the backend Dockerfile into the future, after image creation.
        future = datetime.datetime(2026, 4, 30, tzinfo=datetime.timezone.utc)
        _set_mtime(root / "docker/backend/Dockerfile", future)

        img_time = datetime.datetime(2026, 4, 1, tzinfo=datetime.timezone.utc)
        timestamps = {
            image: img_time.isoformat()
            for image, _ in module.SERVICES.values()
        }
        with _mock_docker_inspect(timestamps):
            module.main()

        flagged = capsys.readouterr().out.strip().splitlines()
        assert flagged == ["backend"]

    def test_requirements_change_flags_scheduler(self, detector, capsys):
        """Scheduler ships a separate requirements.txt — adding a dep there
        without touching the Dockerfile must still trigger a rebuild."""
        module, root = detector
        old = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        for _, file_paths in module.SERVICES.values():
            for rel in file_paths:
                _set_mtime(root / rel, old)

        future = datetime.datetime(2026, 4, 30, tzinfo=datetime.timezone.utc)
        _set_mtime(root / "docker/scheduler/requirements.txt", future)

        img_time = datetime.datetime(2026, 4, 1, tzinfo=datetime.timezone.utc)
        timestamps = {
            image: img_time.isoformat()
            for image, _ in module.SERVICES.values()
        }
        with _mock_docker_inspect(timestamps):
            module.main()

        flagged = capsys.readouterr().out.strip().splitlines()
        assert flagged == ["scheduler"]

    def test_missing_image_flags_service(self, detector, capsys):
        """First-time deploy: no image exists yet → must build."""
        module, _root = detector
        # All images present except backend.
        img_time = datetime.datetime(2026, 4, 1, tzinfo=datetime.timezone.utc)
        timestamps: dict[str, str | None] = {
            image: img_time.isoformat()
            for image, _ in module.SERVICES.values()
        }
        timestamps["trinity-backend:latest"] = None  # simulate missing image

        with _mock_docker_inspect(timestamps):
            module.main()

        flagged = capsys.readouterr().out.strip().splitlines()
        assert "backend" in flagged

    def test_npm_lockfile_change_flags_frontend(self, detector, capsys):
        """A `package-lock.json` bump (no Dockerfile change) must also fire."""
        module, root = detector
        old = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        for _, file_paths in module.SERVICES.values():
            for rel in file_paths:
                _set_mtime(root / rel, old)

        future = datetime.datetime(2026, 4, 30, tzinfo=datetime.timezone.utc)
        _set_mtime(root / "src/frontend/package-lock.json", future)

        img_time = datetime.datetime(2026, 4, 1, tzinfo=datetime.timezone.utc)
        timestamps = {
            image: img_time.isoformat()
            for image, _ in module.SERVICES.values()
        }
        with _mock_docker_inspect(timestamps):
            module.main()

        flagged = capsys.readouterr().out.strip().splitlines()
        assert flagged == ["frontend"]

    def test_handles_nanosecond_precision_timestamp(self, detector, capsys):
        """Docker emits 9-digit fractional seconds; Python's fromisoformat
        accepts at most 6. The detector must trim cleanly."""
        module, root = detector
        old = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        for _, file_paths in module.SERVICES.values():
            for rel in file_paths:
                _set_mtime(root / rel, old)

        # Nanosecond-precision RFC3339 — a real Docker output format.
        ts = "2026-04-13T12:00:00.123456789+00:00"
        timestamps = {
            image: ts for image, _ in module.SERVICES.values()
        }
        with _mock_docker_inspect(timestamps):
            rc = module.main()

        assert rc == 0
        flagged = capsys.readouterr().out.strip()
        assert flagged == "", "Nothing should be flagged when files are old"
