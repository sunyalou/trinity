import os
import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STARTUP_SH = REPO_ROOT / "docker" / "base-image" / "startup.sh"


def _copy_startup_harness(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "workspace"
    clone_dir = tmp_path / "repo-clone"
    workspace.mkdir()
    script = tmp_path / "startup.sh"
    content = STARTUP_SH.read_text()
    content = content.replace("/home/developer", str(workspace))
    content = content.replace("/tmp/repo-clone", str(clone_dir))
    content = content.replace("/tmp/.local.bak", str(tmp_path / ".local.bak"))
    content = content.replace(
        "# Initialize from local template if specified (fallback)",
        "exit 0\n# Initialize from local template if specified (fallback)",
    )
    script.write_text(content)
    script.chmod(0o755)
    return script, workspace, clone_dir


def _write_git_stub(tmp_path: Path, fixture: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text(
        f"""#!/bin/bash
set -e
if [ "$1" = "clone" ]; then
  dest="${{@:$#:1}}"
  rm -rf "$dest"
  mkdir -p "$dest"
  cp -a "{fixture}/." "$dest/"
  exit 0
fi
exit 0
"""
    )
    git.chmod(0o755)
    return bin_dir


def _run_startup(tmp_path: Path, fixture: Path, template_path: str | None = None) -> tuple[subprocess.CompletedProcess, Path]:
    script, workspace, _clone_dir = _copy_startup_harness(tmp_path)
    bin_dir = _write_git_stub(tmp_path, fixture)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GITHUB_REPO": "owner/repo",
        "GITHUB_PAT": "token",
        "GIT_SOURCE_BRANCH": "main",
        "ENABLE_SSH": "false",
        "ENABLE_AGENT_UI": "false",
    }
    if template_path is not None:
        env["GITHUB_TEMPLATE_PATH"] = template_path
    result = subprocess.run([str(script)], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=10)
    return result, workspace


def _valid_template(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "template.yaml").write_text("name: test\n")
    (path / "AGENTS.md").write_text("instructions\n")


def test_root_template_copy_behavior_remains_unchanged(tmp_path):
    fixture = tmp_path / "fixture"
    _valid_template(fixture)
    (fixture / "root.txt").write_text("root\n")

    result, workspace = _run_startup(tmp_path, fixture)

    assert result.returncode == 0, result.stderr + result.stdout
    assert (workspace / "template.yaml").read_text() == "name: test\n"
    assert (workspace / "AGENTS.md").read_text() == "instructions\n"
    assert (workspace / "root.txt").read_text() == "root\n"
    assert (workspace / ".trinity-initialized").exists()


def test_subdirectory_template_copies_only_selected_directory_with_dotfiles_and_spaces(tmp_path):
    fixture = tmp_path / "fixture"
    selected = fixture / "research-agent"
    _valid_template(selected)
    (selected / "file with spaces.md").write_text("spaces\n")
    (selected / ".hidden").write_text("hidden\n")
    other = fixture / "other-agent"
    _valid_template(other)
    (other / "other.txt").write_text("other\n")

    result, workspace = _run_startup(tmp_path, fixture, "research-agent")

    assert result.returncode == 0, result.stderr + result.stdout
    assert (workspace / "template.yaml").exists()
    assert (workspace / "AGENTS.md").exists()
    assert (workspace / "file with spaces.md").read_text() == "spaces\n"
    assert (workspace / ".hidden").read_text() == "hidden\n"
    assert not (workspace / "other-agent").exists()
    assert not (workspace / "other.txt").exists()


def test_missing_subdirectory_writes_template_path_missing(tmp_path):
    fixture = tmp_path / "fixture"
    _valid_template(fixture)

    result, workspace = _run_startup(tmp_path, fixture, "missing-agent")

    assert result.returncode == 0, result.stderr + result.stdout
    assert "template_path_missing" in (workspace / ".git-clone-status").read_text()


def test_subdirectory_missing_template_yaml_writes_template_path_invalid(tmp_path):
    fixture = tmp_path / "fixture"
    selected = fixture / "research-agent"
    selected.mkdir(parents=True)
    (selected / "AGENTS.md").write_text("instructions\n")

    result, workspace = _run_startup(tmp_path, fixture, "research-agent")

    assert result.returncode == 0, result.stderr + result.stdout
    assert "template_path_invalid" in (workspace / ".git-clone-status").read_text()


def test_subdirectory_missing_instruction_file_writes_template_path_invalid(tmp_path):
    fixture = tmp_path / "fixture"
    selected = fixture / "research-agent"
    selected.mkdir(parents=True)
    (selected / "template.yaml").write_text("name: test\n")

    result, workspace = _run_startup(tmp_path, fixture, "research-agent")

    assert result.returncode == 0, result.stderr + result.stdout
    assert "template_path_invalid" in (workspace / ".git-clone-status").read_text()


def test_startup_does_not_use_eval_for_clone_commands():
    content = STARTUP_SH.read_text()
    assert 'eval "${CLONE_CMD}"' not in content
    assert 'eval "${SHALLOW_CLONE_CMD}"' not in content
