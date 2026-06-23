from __future__ import annotations

from dataclasses import dataclass
import re


_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")


@dataclass(frozen=True)
class GitHubTemplateRef:
    canonical: str
    repo: str
    template_path: str | None = None
    branch: str | None = None

    @property
    def template_id(self) -> str:
        return f"github:{self.canonical}"


def _validate_branch(branch: str) -> str:
    if not branch:
        raise ValueError("Branch must not be empty")
    if not _BRANCH_RE.match(branch):
        raise ValueError("Branch contains unsupported characters")
    if ".." in branch or branch.startswith("/") or branch.endswith("/"):
        raise ValueError("Branch must be relative and normalized")
    if "@{" in branch or "\\" in branch or "//" in branch:
        raise ValueError("Branch contains unsupported git ref syntax")
    if any(part.endswith(".lock") for part in branch.split("/")):
        raise ValueError("Branch contains unsupported .lock component")
    return branch


def _validate_template_path(path: str) -> str:
    if not path:
        raise ValueError("Template path must not be empty")
    if path.startswith("/") or "\\" in path or "@" in path:
        raise ValueError("Template path contains unsupported characters")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("Template path must be relative and normalized")
    if any(part.strip() != part or any(ch.isspace() for ch in part) for part in parts):
        raise ValueError("Template path must not contain whitespace")
    return "/".join(parts)


def parse_github_template_ref(raw: str) -> GitHubTemplateRef:
    value = (raw or "").strip()
    if value.startswith("github:"):
        value = value[len("github:") :]
    if not value:
        raise ValueError("GitHub template ref is required")

    branch = None
    if "@" in value:
        value, branch = value.rsplit("@", 1)
        branch = _validate_branch(branch)

    template_path = None
    if "//" in value:
        repo, template_path = value.split("//", 1)
        template_path = _validate_template_path(template_path)
    else:
        repo = value

    if not _REPO_RE.match(repo):
        raise ValueError("GitHub template repo must use owner/repo format")

    canonical = repo
    if template_path:
        canonical += f"//{template_path}"
    if branch:
        canonical += f"@{branch}"
    return GitHubTemplateRef(canonical=canonical, repo=repo, template_path=template_path, branch=branch)
