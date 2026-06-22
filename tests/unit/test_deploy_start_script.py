from pathlib import Path


def test_start_script_rebuilds_backend_with_fresh_provenance():
    """start.sh must rebuild so exported build metadata reaches /api/version."""
    script = Path("scripts/deploy/start.sh").read_text()

    assert "export GIT_COMMIT=$(git rev-parse HEAD)" in script
    assert "export BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)" in script
    assert "docker compose up -d --build" in script
