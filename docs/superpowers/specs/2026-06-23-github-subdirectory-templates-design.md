# GitHub Subdirectory Templates Design

Date: 2026-06-23
Status: Draft for review

## Goal

Support GitHub template repositories that contain multiple Trinity templates in subdirectories. This lets users maintain one template collection repository such as `/Users/yalou/src/trinity-agent-templates` with this shape:

```text
trinity-agent-templates/
  README.md
  research-agent/
    template.yaml
    AGENTS.md
    CLAUDE.md
    GEMINI.md
    README.md
  market-analyst/
    template.yaml
    AGENTS.md
    CLAUDE.md
    GEMINI.md
    README.md
```

Trinity should be able to list and create an agent from a specific subdirectory template.

## User-Facing Syntax

Keep existing GitHub template syntax working:

```text
github:owner/repo
github:owner/repo@branch
```

Add subdirectory syntax:

```text
github:owner/repo//template-path
github:owner/repo//template-path@branch
```

Settings → GitHub Templates should accept the same reference without the `github:` prefix:

```text
owner/repo
owner/repo//template-path
owner/repo//template-path@branch
```

Examples:

```text
github:sunyalou/trinity-agent-templates//research-agent
github:sunyalou/trinity-agent-templates//market-analyst@main
sunyalou/trinity-agent-templates//tech-researcher
```

## Parsing Rules

Introduce a single parser for GitHub template references.

Inputs:

- optional `github:` prefix
- repository: `owner/repo`
- optional template path after `//`
- optional branch suffix after the last `@`

Outputs:

```python
GitHubTemplateRef(
    canonical="owner/repo//template-path@branch",
    repo="owner/repo",
    template_path="template-path" | None,
    branch="branch" | None,
)
```

Validation:

- `repo` must remain `owner/repo` with the existing owner/repo character set.
- `template_path` is optional.
- `template_path` must be relative, normalized, and must not contain empty segments, `.` segments, `..` segments, backslashes, or a leading slash.
- `template_path` must not contain `@`. V1 does not define escaping.
- `branch` is optional but, if present, must be strictly validated and rejected on failure. Invalid branch text must never be silently ignored.
- `branch` must reject empty values, whitespace, shell metacharacters, `..`, leading `/`, trailing `/`, `.lock`, `@{`, backslashes, control characters, and values above a bounded maximum length.
- Valid existing refs without `//` behave as before. Malformed branch refs now fail closed instead of being silently normalized or ignored.

The parser must be the single source of truth for Settings validation, template metadata lookup, agent creation, and any environment values propagated to startup.

Startup currently builds clone commands with shell strings. The implementation should remove `eval` from clone execution or otherwise quote validated branch/repo values safely. Passing parser output into an `eval` command string is not acceptable for new subdirectory template support.

## Template Metadata Fetching

Currently Trinity fetches:

```text
GET /repos/{owner}/{repo}/contents/template.yaml
```

For subdirectory templates, fetch:

```text
GET /repos/{owner}/{repo}/contents/{template_path}/template.yaml
```

Branch handling should be preserved. When a branch is present, the GitHub contents request should include the branch ref.

Metadata cache keys must include the normalized repo, template path, and branch. Root templates, different subdirectories, and different branches must not share cache entries. Acceptable cache keys include:

```python
(repo, template_path, branch)
```

or a canonical string such as:

```text
owner/repo//path@branch
```

The GitHub Contents API path must be built from validated path segments. URL-encode each segment independently and join with `/`; do not URL-encode the whole path as one string and do not allow template paths to alter the API route or query string.

Template list entries should keep their current response shape and add optional metadata fields:

```json
{
  "id": "github:owner/repo//template-path@branch",
  "github_repo": "owner/repo//template-path@branch",
  "repo": "owner/repo",
  "clone_repo": "owner/repo",
  "source": "github",
  "template_path": "template-path",
  "source_branch": "branch"
}
```

For root templates, `template_path` should be omitted or `null`.

Canonical IDs must include the branch when a branch is present, because the frontend passes the selected template `id` as the create-agent `template` payload. Branch-specific templates must not rely on side-channel fields during creation. Examples:

```json
{
  "id": "github:owner/repo//template-path@branch",
  "github_repo": "owner/repo//template-path@branch",
  "repo": "owner/repo",
  "clone_repo": "owner/repo",
  "template_path": "template-path",
  "source_branch": "branch"
}
```

```json
{
  "id": "github:owner/repo@branch",
  "github_repo": "owner/repo@branch",
  "repo": "owner/repo",
  "clone_repo": "owner/repo",
  "template_path": null,
  "source_branch": "branch"
}
```

For refs without a branch, canonical IDs omit `@branch`.

Field semantics:

- `github_repo` remains the user-facing configured template reference for API/backward compatibility.
- `repo`/`clone_repo` is the cloneable repository name and must never include `//template-path`.
- Agent creation and startup env generation must use parsed `repo`/`clone_repo`, not raw `github_repo`.

## Agent Creation Flow

When creating from `github:owner/repo//template-path[@branch]`:

1. Parse the template reference.
2. Validate access to `owner/repo` as before.
3. Fetch metadata from `{template_path}/template.yaml` for UI/listing and resource defaults.
4. Pass the base repo and branch to the agent container as before.
5. Pass the template subdirectory to the agent container via a new environment variable:

```text
GITHUB_TEMPLATE_PATH=template-path
```

The backend should continue to pass:

```text
GITHUB_REPO=owner/repo
GIT_SOURCE_BRANCH=branch-or-main
```

It should not put `//template-path` inside `GITHUB_REPO`, because startup clone URLs must remain valid Git URLs.

The current `crud.py` GitHub-template flow has ad hoc parsing and a mostly-always-successful `get_github_template()` dynamic fallback. Replace that flow with the shared parser before metadata lookup and before setting `github_repo_for_agent`. The dynamic path must not treat `owner/repo//path` as a clone repo.

For subdirectory refs, the backend must treat the template as a one-time initialization source:

- Do not call `reserve_and_generate_instance_id`.
- Do not create a git config DB row for the subdirectory template source.
- Do not set `GIT_SYNC_ENABLED=true`.
- Do not set `GIT_SYNC_AUTO`.
- Do not set `GIT_WORKING_BRANCH`.
- Do set `GITHUB_REPO=owner/repo`.
- Do set `GITHUB_PAT=...`.
- Do set `GIT_SOURCE_BRANCH=branch-or-main`.
- Do set `GITHUB_TEMPLATE_PATH=template-path`.

Root GitHub templates should retain existing git-sync behavior unless separately changed.

## Agent Startup Copy Behavior

The agent startup script currently clones the GitHub repo and copies the repository root into `/home/developer` for non-sync clones.

New behavior:

- If `GITHUB_TEMPLATE_PATH` is unset, preserve existing behavior.
- If `GITHUB_TEMPLATE_PATH` is set, clone the repo as before, then copy only the subdirectory contents into `/home/developer`.
- The resulting agent workspace must have `template.yaml` and runtime instruction files at its root.
- If the subdirectory does not exist, write a clone/init status error and do not silently create a blank agent.
- Before copying a subdirectory, startup must verify that `$GITHUB_TEMPLATE_PATH` exists, is a directory, contains `template.yaml`, and contains at least one supported instruction file (`AGENTS.md`, `CLAUDE.md`, or `GEMINI.md`). Because Trinity currently requires `CLAUDE.md` for some compatibility validation paths, templates should still include it, but startup should not assume every runtime uses it as the only instruction file.
- Missing or invalid subdirectories should write a distinct `.git-clone-status` error such as `template_path_missing` or `template_path_invalid`.

The local-template path behavior is unchanged.

## Git Sync Scope

Subdirectory templates are source templates, not bidirectional sync roots in v1.

Reason: if Trinity copies only a subdirectory into the agent workspace, that workspace is no longer a normal clone of the original repository root. Pushing changes back into the monorepo subdirectory requires sparse checkout/subtree semantics and separate conflict handling.

V1 rule:

- `github:owner/repo//template-path` should initialize the agent from the subdirectory.
- It should not enable bidirectional git sync back to the source monorepo subdirectory.
- Omit the git config DB row for subdirectory template refs.
- Skip working branch reservation for subdirectory refs.

Future work can add sparse-checkout/subtree sync.

## Settings API

`GET /api/settings/github-templates` and `PUT /api/settings/github-templates` should accept subdirectory refs in `github_repo` for compatibility with current DB shape.

Validation should accept:

```text
owner/repo
owner/repo@branch
owner/repo//path
owner/repo//path@branch
```

The response should continue to include `github_repo`, plus resolved display metadata from the correct `template.yaml` path.

No database migration is required.

Settings stores the full configured template ref string in the existing `github_repo` field. This field is catalog configuration only. Agent creation must parse that ref and pass only the parsed clone repo into `GITHUB_REPO`.

Frontend validation must be updated wherever custom GitHub template refs are accepted:

- Create Agent custom template input should accept `owner/repo@branch`, `owner/repo//path`, and `owner/repo//path@branch`.
- Settings → GitHub Templates input should accept the same.
- Duplicate detection should use canonicalized refs so equivalent refs cannot be added twice.
- Invalid refs should show clear validation errors instead of being silently normalized.

## Template Repository Restructure

Restructure `/Users/yalou/src/trinity-agent-templates` from a single root template into a multi-template repository:

```text
/Users/yalou/src/trinity-agent-templates/
  README.md
  research-agent/
    template.yaml
    AGENTS.md
    CLAUDE.md
    GEMINI.md
    README.md
    .gitignore
    reports/.gitkeep
    requirements.txt
    tests/README.md
    tests/validate_template.py
```

The root README should explain how to add a subdirectory template to Trinity:

```text
sunyalou/trinity-agent-templates//research-agent
```

Each template subdirectory should remain independently valid when used as the template root.

## Testing

Backend unit tests should cover:

- parsing root refs
- parsing subdirectory refs
- parsing branch refs
- rejecting path traversal and malformed paths
- rejecting empty subdirectory paths such as `owner/repo//`
- rejecting duplicate slash paths such as `owner/repo//a//b`
- rejecting path segments containing `@`
- rejecting empty branch refs such as `owner/repo//path@`
- rejecting branch values with shell metacharacters instead of silently ignoring them
- fetching `template.yaml` from repo root for root refs
- fetching `template.yaml` from subdirectory for subdirectory refs
- cache separation for `owner/repo`, `owner/repo//a`, `owner/repo//b`, `owner/repo//a@main`, and `owner/repo//a@dev`
- settings validation accepts `owner/repo//path`
- settings validation accepts `owner/repo@branch`
- agent creation passes `GITHUB_REPO=owner/repo` and `GITHUB_TEMPLATE_PATH=path`
- agent creation does not create git-sync DB rows or git-sync env vars for subdirectory refs

Frontend tests should cover:

- Create Agent custom input accepts `owner/repo//path`.
- Create Agent custom input accepts `owner/repo//path@branch`.
- Create Agent custom input accepts `owner/repo@branch`.
- Settings GitHub Templates input accepts `owner/repo//path`.
- Duplicate detection uses canonical refs.

Startup-script tests should cover:

- root template copy behavior remains unchanged
- subdirectory template copy copies only that directory's contents
- missing subdirectory produces an explicit failure status
- invalid subdirectory missing `template.yaml` produces an explicit failure status
- subdirectory copy uses robust copying that handles filenames with spaces and dotfiles, instead of fragile `for item in $(ls -A ...)` loops.

Template repository validation should run from the subdirectory:

```bash
cd /Users/yalou/src/trinity-agent-templates/research-agent
python tests/validate_template.py
```

## Acceptance Criteria

- Existing `github:owner/repo` templates continue to work.
- Valid existing root branch refs such as `github:owner/repo@branch` continue to work, but malformed branch refs now fail closed instead of being silently normalized or ignored.
- `github:owner/repo//path` appears in template listings with metadata from `path/template.yaml`.
- Branch-specific template listings preserve the branch in `id` and `github_repo`, so creation receives the same canonical ref that was listed.
- Creating an agent from `github:owner/repo//path` puts that path's files at the agent workspace root.
- Invalid paths such as `../x`, `/x`, `a//b`, and `a/../b` are rejected.
- `/Users/yalou/src/trinity-agent-templates` is organized as a multi-template repo with the current research template under `research-agent/`.
- Final validation passes for both Trinity tests and the moved research template.
