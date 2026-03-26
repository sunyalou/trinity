"""
Unit tests for git pull branch detection logic.

Tests the _get_pull_branch() helper and verifies that git status/pull/sync
endpoints correctly target origin/main for trinity/* working branches.

Covers fix for GitHub issue #195.
"""

import subprocess
import tempfile
from pathlib import Path

import pytest


def _get_pull_branch(current_branch: str, home_dir: Path) -> str:
    """Mirror of agent_server.routers.git._get_pull_branch for testing.

    The actual function lives in the agent container image and can't be imported
    directly due to relative imports. This mirror must stay in sync with the
    source at docker/base-image/agent_server/routers/git.py.
    """
    if not current_branch.startswith("trinity/"):
        return current_branch
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "origin/main"],
        capture_output=True, text=True, cwd=str(home_dir), timeout=10
    )
    return "main" if result.returncode == 0 else current_branch


class TestGetPullBranch:
    """Unit tests for _get_pull_branch() helper function."""

    def setup_method(self):
        """Create a temporary git repo for each test."""
        self.tmpdir = tempfile.mkdtemp()
        self.home_dir = Path(self.tmpdir)

        # Initialize a bare "remote" repo
        self.remote_dir = tempfile.mkdtemp()
        subprocess.run(
            ["git", "init", "--bare"],
            cwd=self.remote_dir,
            capture_output=True,
            timeout=10
        )

        # Initialize the local repo
        subprocess.run(["git", "init"], cwd=self.tmpdir, capture_output=True, timeout=10)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=self.tmpdir, capture_output=True, timeout=10
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=self.tmpdir, capture_output=True, timeout=10
        )
        subprocess.run(
            ["git", "remote", "add", "origin", self.remote_dir],
            cwd=self.tmpdir, capture_output=True, timeout=10
        )

        # Create initial commit and push to establish origin/main
        (self.home_dir / "README.md").write_text("test")
        subprocess.run(["git", "add", "."], cwd=self.tmpdir, capture_output=True, timeout=10)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=self.tmpdir, capture_output=True, timeout=10
        )
        subprocess.run(
            ["git", "push", "-u", "origin", "main"],
            cwd=self.tmpdir, capture_output=True, timeout=10
        )

    def test_trinity_branch_returns_main(self):
        """trinity/* branch with origin/main should return 'main'."""
        subprocess.run(
            ["git", "checkout", "-b", "trinity/my-agent/abc123"],
            cwd=self.tmpdir, capture_output=True, timeout=10
        )
        result = _get_pull_branch("trinity/my-agent/abc123", self.home_dir)
        assert result == "main"

    def test_trinity_branch_no_origin_main_returns_current(self):
        """trinity/* branch without origin/main falls back to current branch."""
        # Remove origin so origin/main doesn't exist
        subprocess.run(
            ["git", "remote", "remove", "origin"],
            cwd=self.tmpdir, capture_output=True, timeout=10
        )
        subprocess.run(
            ["git", "remote", "add", "origin", "https://example.com/nonexistent.git"],
            cwd=self.tmpdir, capture_output=True, timeout=10
        )

        result = _get_pull_branch("trinity/my-agent/abc123", self.home_dir)
        assert result == "trinity/my-agent/abc123"

    def test_non_trinity_branch_returns_current(self):
        """Non-trinity branches should return unchanged."""
        result = _get_pull_branch("main", self.home_dir)
        assert result == "main"

    def test_non_trinity_feature_branch_returns_current(self):
        """Feature branches should return unchanged."""
        result = _get_pull_branch("feature/my-feature", self.home_dir)
        assert result == "feature/my-feature"

    def test_trinity_branch_deep_nesting(self):
        """trinity/ prefix with multiple path segments should still return main."""
        result = _get_pull_branch("trinity/agent-name/instance-id", self.home_dir)
        assert result == "main"


class TestGitPullFromMainEndToEnd:
    """End-to-end test: verify pull detects upstream changes on main.

    Simulates the real scenario:
    1. Clone a repo, create a working branch trinity/...
    2. Push a new commit to main on the "remote"
    3. Fetch and check behind count against origin/main
    4. Pull from origin/main into the working branch
    """

    def setup_method(self):
        """Set up a local repo with remote, mimicking a Trinity agent."""
        # Create a bare "remote" (like GitHub)
        self.remote_dir = tempfile.mkdtemp()
        subprocess.run(
            ["git", "init", "--bare"],
            cwd=self.remote_dir,
            capture_output=True, timeout=10
        )

        # Create a "clone" (like the agent container)
        self.agent_dir = tempfile.mkdtemp()
        subprocess.run(
            ["git", "clone", self.remote_dir, self.agent_dir],
            capture_output=True, timeout=10
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=self.agent_dir, capture_output=True, timeout=10
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=self.agent_dir, capture_output=True, timeout=10
        )

        # Create initial commit on main
        Path(self.agent_dir, "README.md").write_text("initial content")
        subprocess.run(["git", "add", "."], cwd=self.agent_dir, capture_output=True, timeout=10)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=self.agent_dir, capture_output=True, timeout=10
        )
        subprocess.run(
            ["git", "push", "-u", "origin", "main"],
            cwd=self.agent_dir, capture_output=True, timeout=10
        )

        # Create a working branch (like Trinity does)
        subprocess.run(
            ["git", "checkout", "-b", "trinity/test-agent/abc123"],
            cwd=self.agent_dir, capture_output=True, timeout=10
        )
        subprocess.run(
            ["git", "push", "-u", "origin", "trinity/test-agent/abc123"],
            cwd=self.agent_dir, capture_output=True, timeout=10
        )

    def _push_commit_to_main(self):
        """Push a new commit to main on the remote (simulating GitHub push)."""
        # Create a separate clone to push to main
        pusher_dir = tempfile.mkdtemp()
        subprocess.run(
            ["git", "clone", self.remote_dir, pusher_dir],
            capture_output=True, timeout=10
        )
        subprocess.run(
            ["git", "config", "user.email", "dev@example.com"],
            cwd=pusher_dir, capture_output=True, timeout=10
        )
        subprocess.run(
            ["git", "config", "user.name", "Developer"],
            cwd=pusher_dir, capture_output=True, timeout=10
        )
        Path(pusher_dir, "new-file.txt").write_text("upstream change")
        subprocess.run(["git", "add", "."], cwd=pusher_dir, capture_output=True, timeout=10)
        subprocess.run(
            ["git", "commit", "-m", "upstream update"],
            cwd=pusher_dir, capture_output=True, timeout=10
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=pusher_dir, capture_output=True, timeout=10
        )

    def test_detects_upstream_changes_on_main(self):
        """After pushing to main, agent on working branch should see commits behind."""
        self._push_commit_to_main()

        # Fetch in agent (like the fixed status endpoint does)
        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=self.agent_dir, capture_output=True, timeout=10
        )

        # Determine pull branch
        pull_branch = _get_pull_branch("trinity/test-agent/abc123", Path(self.agent_dir))
        assert pull_branch == "main"

        # Check behind count against origin/main
        result = subprocess.run(
            ["git", "rev-list", "--count", f"HEAD..origin/{pull_branch}"],
            cwd=self.agent_dir, capture_output=True, text=True, timeout=10
        )
        behind_count = int(result.stdout.strip())
        assert behind_count == 1, f"Expected 1 commit behind, got {behind_count}"

    def test_pull_from_main_brings_changes(self):
        """Pulling from origin/main should bring upstream changes into working branch."""
        self._push_commit_to_main()

        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=self.agent_dir, capture_output=True, timeout=10
        )

        pull_branch = _get_pull_branch("trinity/test-agent/abc123", Path(self.agent_dir))

        # Pull from main
        result = subprocess.run(
            ["git", "pull", "--rebase", "origin", pull_branch],
            cwd=self.agent_dir, capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, f"Pull failed: {result.stderr}"

        # Verify the file from the upstream commit exists
        assert Path(self.agent_dir, "new-file.txt").exists()

    def test_old_behavior_misses_changes(self):
        """Without the fix, checking origin/{working_branch} shows 0 behind."""
        self._push_commit_to_main()

        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=self.agent_dir, capture_output=True, timeout=10
        )

        # Old behavior: check against origin/{current_branch}
        current_branch = "trinity/test-agent/abc123"
        result = subprocess.run(
            ["git", "rev-list", "--count", f"HEAD..origin/{current_branch}"],
            cwd=self.agent_dir, capture_output=True, text=True, timeout=10
        )
        # The old behavior: 0 behind (because nobody pushed to origin/trinity/...)
        behind_count = int(result.stdout.strip())
        assert behind_count == 0, "Old behavior should show 0 behind (bug confirmed)"

    def test_force_reset_to_main(self):
        """Force reset to origin/main should bring working branch to main's state."""
        self._push_commit_to_main()

        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=self.agent_dir, capture_output=True, timeout=10
        )

        pull_branch = _get_pull_branch("trinity/test-agent/abc123", Path(self.agent_dir))

        result = subprocess.run(
            ["git", "reset", "--hard", f"origin/{pull_branch}"],
            cwd=self.agent_dir, capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0

        # Verify the upstream file exists
        assert Path(self.agent_dir, "new-file.txt").exists()
