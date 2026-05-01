#!/usr/bin/env python3
"""Print platform services whose Docker images are stale (#557).

For each service in ``SERVICES``, compare the image's ``.Created``
timestamp against the latest mtime among the service's tracked files
(Dockerfile, requirements.txt, package.json, package-lock.json). Stale
service names go to stdout, one per line; human-readable reasons go to
stderr. ``start.sh`` consumes stdout to decide what to ``docker compose
build`` before ``up -d``.

Tracked files are the ones whose changes imply a new image is required
— Dockerfiles (because they pin installed deps inline) and dependency
manifests that the Dockerfile copies in. Plain source files are not
tracked: source is bind-mounted at runtime, so source-only changes do
not require a rebuild.

Exit code is always 0 — a failure here must not block ``start.sh``.
"""
from __future__ import annotations

import datetime
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Each entry: service name → (image:tag, [files whose changes imply rebuild]).
# Paths are relative to the project root.
SERVICES: dict[str, tuple[str, list[str]]] = {
    "backend": (
        "trinity-backend:latest",
        [
            "docker/backend/Dockerfile",
        ],
    ),
    "frontend": (
        "trinity-frontend:latest",
        [
            "docker/frontend/Dockerfile",
            "src/frontend/package.json",
            "src/frontend/package-lock.json",
        ],
    ),
    "mcp-server": (
        "trinity-mcp-server:latest",
        [
            "src/mcp-server/Dockerfile",
            "src/mcp-server/package.json",
            "src/mcp-server/package-lock.json",
        ],
    ),
    "scheduler": (
        "trinity-scheduler:latest",
        [
            "docker/scheduler/Dockerfile",
            "docker/scheduler/requirements.txt",
        ],
    ),
}


def _image_created_at(image: str) -> datetime.datetime | None:
    """Return the image's creation time, or ``None`` if the image is missing.

    Docker emits RFC 3339 with timezone offset and nanosecond precision
    (e.g. ``2026-04-13T12:00:00.123456789+00:00``). ``fromisoformat``
    handles the offset in Python 3.11+ but only accepts ≤6 fractional
    digits, so trim nanos defensively.
    """
    try:
        out = subprocess.check_output(
            ["docker", "image", "inspect", image, "--format", "{{.Created}}"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    if not out:
        return None
    if "." in out:
        head, _, tail = out.partition(".")
        # tail looks like "123456789+00:00" or "123456789Z"
        for sep in ("+", "-", "Z"):
            if sep in tail:
                idx = tail.index(sep)
                frac, suffix = tail[:idx], tail[idx:]
                out = f"{head}.{frac[:6]}{suffix}"
                break
        else:
            out = f"{head}.{tail[:6]}"
    try:
        return datetime.datetime.fromisoformat(out)
    except ValueError:
        return None


def _file_mtime(path: Path) -> datetime.datetime | None:
    try:
        ts = path.stat().st_mtime
    except OSError:
        return None
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)


def main() -> int:
    for service, (image, file_paths) in SERVICES.items():
        img_dt = _image_created_at(image)
        if img_dt is None:
            print(
                f"  {service}: image {image} missing → will build",
                file=sys.stderr,
            )
            print(service)
            continue

        for rel in file_paths:
            f_dt = _file_mtime(PROJECT_ROOT / rel)
            if f_dt is None:
                continue
            if f_dt > img_dt:
                print(
                    f"  {service}: {rel} modified at {f_dt.isoformat()} "
                    f"after image build at {img_dt.isoformat()}",
                    file=sys.stderr,
                )
                print(service)
                break

    return 0


if __name__ == "__main__":
    sys.exit(main())
