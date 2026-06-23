# GitHub Subdirectory Templates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Trinity support for GitHub template refs like `github:owner/repo//template-path[@branch]` and reorganize `/Users/yalou/src/trinity-agent-templates` as a multi-template repository.

**Architecture:** Add a shared parser for GitHub template refs, use it for metadata lookup, settings validation, agent creation, frontend validation, and startup environment propagation. Subdirectory templates are one-time initialization sources: backend passes `GITHUB_REPO=owner/repo` plus `GITHUB_TEMPLATE_PATH=path`, and startup clones the repo then copies only that subdirectory into the agent workspace.

**Tech Stack:** Python/FastAPI/Pydantic backend, Bash startup script, Vue frontend, pytest unit tests, Node/Vite frontend tests where available, Git repository restructure.

---

## File Structure

Trinity repository changes:

```text
/Users/yalou/src/trinity/
  src/backend/services/github_template_ref.py      # new shared parser/canonicalizer
  src/backend/services/template_service.py         # metadata fetch/cache path/branch support
  src/backend/services/agent_service/crud.py       # use parsed refs, disable git sync for subdir refs
  src/backend/routers/settings.py                  # settings validation accepts canonical refs
  docker/base-image/startup.sh                     # copy subdir templates safely, remove new eval usage
  src/frontend/src/utils/githubTemplateRefs.js     # new frontend parser/canonicalizer
  src/frontend/src/components/CreateAgentModal.vue # custom input accepts subdir refs
  src/frontend/src/views/Settings.vue              # settings input accepts/dedupes subdir refs
  tests/unit/test_github_template_ref.py           # parser tests
  tests/unit/test_github_subdirectory_templates.py # metadata/cache/creation tests
  tests/unit/test_startup_github_template_path.py  # executable startup copy behavior checks
```

Template repository changes:

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
    requirements.txt
    reports/.gitkeep
    tests/README.md
    tests/validate_template.py
```

## Task 1: Shared GitHub Template Ref Parser

**Files:**
- Create: `/Users/yalou/src/trinity/src/backend/services/github_template_ref.py`
- Create: `/Users/yalou/src/trinity/tests/unit/test_github_template_ref.py`

- [ ] **Step 1: Write parser tests first**

Create `/Users/yalou/src/trinity/tests/unit/test_github_template_ref.py`:

```python
import pytest

from services.github_template_ref import GitHubTemplateRef, parse_github_template_ref


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("owner/repo", GitHubTemplateRef(canonical="owner/repo", repo="owner/repo", template_path=None, branch=None)),
        ("github:owner/repo", GitHubTemplateRef(canonical="owner/repo", repo="owner/repo", template_path=None, branch=None)),
        ("owner/repo@dev", GitHubTemplateRef(canonical="owner/repo@dev", repo="owner/repo", template_path=None, branch="dev")),
        ("owner/repo//research-agent", GitHubTemplateRef(canonical="owner/repo//research-agent", repo="owner/repo", template_path="research-agent", branch=None)),
        ("github:owner/repo//research-agent@main", GitHubTemplateRef(canonical="owner/repo//research-agent@main", repo="owner/repo", template_path="research-agent", branch="main")),
        ("owner/repo//nested/template@release/v1", GitHubTemplateRef(canonical="owner/repo//nested/template@release/v1", repo="owner/repo", template_path="nested/template", branch="release/v1")),
    ],
)
def test_parse_valid_refs(raw, expected):
    assert parse_github_template_ref(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "owner",
        "owner/repo/extra",
        "owner/repo//",
        "owner/repo///x",
        "owner/repo//a//b",
        "owner/repo//../x",
        "owner/repo//a/../b",
        "owner/repo///absolute",
        "owner/repo//a@b@main",
        "owner/repo//a b",
        "owner/repo@",
        "owner/repo@bad branch",
        "owner/repo@bad;branch",
        "owner/repo@../branch",
        "owner/repo@branch.lock",
        "owner/repo@feature/foo.lock/bar",
        "owner/repo@foo//bar",
        "owner/repo@bad@{thing}",
        "owner/repo@bad\\branch",
    ],
)
def test_parse_rejects_invalid_refs(raw):
    with pytest.raises(ValueError):
        parse_github_template_ref(raw)


def test_prefixed_canonical_omits_github_prefix():
    ref = parse_github_template_ref("github:owner/repo//research-agent@main")
    assert ref.canonical == "owner/repo//research-agent@main"
    assert ref.template_id == "github:owner/repo//research-agent@main"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
PYTHONPATH=src/backend python -m pytest tests/unit/test_github_template_ref.py -q
```

Expected: FAIL because `services.github_template_ref` does not exist.

- [ ] **Step 3: Implement parser**

Create `/Users/yalou/src/trinity/src/backend/services/github_template_ref.py`:

```python
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
        value = value[len("github:"):]
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
```

- [ ] **Step 4: Run parser tests**

Run:

```bash
PYTHONPATH=src/backend python -m pytest tests/unit/test_github_template_ref.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit parser**

Run:

```bash
git add src/backend/services/github_template_ref.py tests/unit/test_github_template_ref.py
git commit -m "feat: parse GitHub template refs"
```

## Task 2: Metadata Fetching and Cache Key Support

**Files:**
- Modify: `/Users/yalou/src/trinity/src/backend/services/template_service.py`
- Test: `/Users/yalou/src/trinity/tests/unit/test_github_subdirectory_templates.py`

- [ ] **Step 1: Add metadata tests**

Create `/Users/yalou/src/trinity/tests/unit/test_github_subdirectory_templates.py` with tests that monkeypatch `httpx.Client` and verify:

```python
def test_fetches_subdirectory_template_yaml(monkeypatch):
    # _fetch_template_yaml_ref(parse_github_template_ref("owner/repo//research-agent"), "pat")
    # must request .../repos/owner/repo/contents/research-agent/template.yaml
    # and parse returned YAML.

def test_fetch_url_encodes_each_path_segment(monkeypatch):
    # _fetch_template_yaml_ref(parse_github_template_ref("owner/repo//templates/foo#bar"), "pat")
    # must request .../contents/templates/foo%23bar/template.yaml with each segment joined by '/',
    # not one whole encoded path and not a path that can affect query/route construction.

def test_fetches_branch_ref(monkeypatch):
    # branch refs must pass params={"ref": "dev"} or equivalent.

def test_cache_key_includes_path_and_branch(monkeypatch):
    # owner/repo, owner/repo//a, owner/repo//b, owner/repo//a@main,
    # and owner/repo//a@dev must be separate cache entries.

def test_build_template_preserves_canonical_branch_id():
    # id must be github:owner/repo//research-agent@dev and github_repo owner/repo//research-agent@dev.
```

Use concrete fake response objects; do not call network.

- [ ] **Step 2: Run metadata tests to verify failure**

Run:

```bash
PYTHONPATH=src/backend python -m pytest tests/unit/test_github_subdirectory_templates.py -q
```

Expected: FAIL because helpers are not implemented yet.

- [ ] **Step 3: Implement metadata helpers**

In `template_service.py`:

- import `parse_github_template_ref` and `GitHubTemplateRef`
- change cache to key by canonical ref or tuple
- add `_fetch_template_yaml_ref(ref, pat)` that builds the contents URL from `ref.repo` and `ref.template_path`; URL-encode each validated path segment independently before joining with `/`
- include `params={"ref": ref.branch}` when branch exists
- update `_build_template` to include canonical `id`, user-facing `github_repo`, cloneable `repo`, `clone_repo`, `template_path`, and `source_branch`
- update `get_github_template()` to parse refs and return canonical template objects
- update `_fetch_all_metadata()` to parse configured refs and avoid repo-only cache collisions

- [ ] **Step 4: Run metadata tests**

Run:

```bash
PYTHONPATH=src/backend python -m pytest tests/unit/test_github_template_ref.py tests/unit/test_github_subdirectory_templates.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit metadata support**

Run:

```bash
git add src/backend/services/template_service.py tests/unit/test_github_subdirectory_templates.py
git commit -m "feat: fetch GitHub template metadata from subdirectories"
```

## Task 3: Settings API Validation

**Files:**
- Modify: `/Users/yalou/src/trinity/src/backend/routers/settings.py`
- Test: extend `/Users/yalou/src/trinity/tests/unit/test_github_subdirectory_templates.py`

- [ ] **Step 1: Add settings validation tests**

Add tests that call the validation helper or model path for:

```python
valid = ["owner/repo", "owner/repo@dev", "owner/repo//research-agent", "owner/repo//research-agent@main"]
invalid = ["owner/repo//", "owner/repo//../x", "owner/repo@", "owner/repo@bad branch"]
```

- [ ] **Step 2: Replace regex validation**

In `settings.py`, replace `_REPO_PATTERN` use for GitHub Templates with `parse_github_template_ref(entry.github_repo)`.

Error detail should say:

```text
Invalid repository format: '<value>'. Expected 'owner/repo', 'owner/repo@branch', 'owner/repo//path', or 'owner/repo//path@branch'.
```

When storing, store `parse_github_template_ref(entry.github_repo).canonical` in `github_repo`.

- [ ] **Step 3: Run backend tests**

Run:

```bash
PYTHONPATH=src/backend python -m pytest tests/unit/test_github_template_ref.py tests/unit/test_github_subdirectory_templates.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit settings validation**

Run:

```bash
git add src/backend/routers/settings.py tests/unit/test_github_subdirectory_templates.py
git commit -m "feat: accept subdirectory GitHub template settings"
```

## Task 4: Agent Creation Environment and Git Sync Behavior

**Files:**
- Modify: `/Users/yalou/src/trinity/src/backend/services/agent_service/crud.py`
- Test: extend `/Users/yalou/src/trinity/tests/unit/test_github_subdirectory_templates.py`

- [ ] **Step 1: Add creation behavior tests**

Add tests using monkeypatches/stubs around `create_agent` internals or extracted helper functions to verify:

```python
def test_subdirectory_ref_sets_clone_repo_and_template_path():
    # github:owner/repo//research-agent@main yields env GITHUB_REPO=owner/repo,
    # GITHUB_TEMPLATE_PATH=research-agent, GIT_SOURCE_BRANCH=main.

def test_subdirectory_ref_skips_git_sync_reservation():
    # reserve_and_generate_instance_id is not called for subdir refs.

def test_subdirectory_ref_omits_all_git_sync_side_effects():
    # subdir refs must not create git config DB rows or auto-sync flags,
    # and env must omit GIT_SYNC_ENABLED, GIT_SYNC_AUTO, and GIT_WORKING_BRANCH.

def test_root_ref_keeps_existing_git_sync_behavior():
    # github:owner/repo still reserves git sync as before.
```

If full `create_agent` is too heavy, extract a small helper such as `_resolve_github_template_config(config_template)` and test that helper.

- [ ] **Step 2: Implement creation behavior**

In `crud.py`:

- parse `config.template` with `parse_github_template_ref()` after removing `github:` prefix support via parser
- set `github_repo_for_agent = ref.repo`
- set `github_template_path = ref.template_path`
- set `config.source_branch = ref.branch` when present
- for subdirectory refs, skip `git_service.reserve_and_generate_instance_id(...)`
- introduce an explicit boolean such as `github_template_is_subdir` or `enable_git_sync_for_template`
- gate every git-sync side effect on that flag, including branch reservation, git config rollback/deletion, git-sync env vars, `GIT_SYNC_AUTO`, `GIT_WORKING_BRANCH`, and later `db.set_git_auto_sync_enabled(...)` calls
- when env vars are built, set `GITHUB_TEMPLATE_PATH` only when `github_template_path` is present
- do not set git-sync env vars for subdirectory refs

- [ ] **Step 3: Run tests**

Run:

```bash
PYTHONPATH=src/backend python -m pytest tests/unit/test_github_template_ref.py tests/unit/test_github_subdirectory_templates.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit creation support**

Run:

```bash
git add src/backend/services/agent_service/crud.py tests/unit/test_github_subdirectory_templates.py
git commit -m "feat: initialize agents from GitHub template subdirectories"
```

## Task 5: Startup Script Subdirectory Copy

**Files:**
- Modify: `/Users/yalou/src/trinity/docker/base-image/startup.sh`
- Create/modify: `/Users/yalou/src/trinity/tests/unit/test_startup_github_template_path.py`

- [ ] **Step 1: Add executable startup behavior tests**

Create `/Users/yalou/src/trinity/tests/unit/test_startup_github_template_path.py` with a subprocess harness that copies `startup.sh` to a temp directory, stubs `git`, and executes only the GitHub initialization path against temporary directories. The tests should verify behavior, not just static strings:

```python
def test_root_template_copy_behavior_remains_unchanged(tmp_path):
    # Fake git clone creates root files template.yaml and AGENTS.md in /tmp/repo-clone.
    # Run startup with GITHUB_TEMPLATE_PATH unset.
    # Assert files copied to fake HOME/workspace root.

def test_subdirectory_template_copies_only_selected_directory(tmp_path):
    # Fake git clone creates /tmp/repo-clone/research-agent/template.yaml,
    # /tmp/repo-clone/research-agent/AGENTS.md, /tmp/repo-clone/research-agent/file with spaces.md,
    # /tmp/repo-clone/research-agent/.hidden, and /tmp/repo-clone/other-agent/template.yaml.
    # Run startup with GITHUB_TEMPLATE_PATH=research-agent.
    # Assert selected files including dotfiles and filenames with spaces are copied to workspace root.
    # Assert other-agent is not copied.

def test_missing_subdirectory_writes_template_path_missing(tmp_path):
    # Fake git clone omits the requested directory.
    # Assert .git-clone-status contains template_path_missing.

def test_subdirectory_missing_template_yaml_writes_template_path_invalid(tmp_path):
    # Fake git clone creates requested dir and AGENTS.md but no template.yaml.
    # Assert .git-clone-status contains template_path_invalid.

def test_subdirectory_missing_instruction_file_writes_template_path_invalid(tmp_path):
    # Fake git clone creates requested dir and template.yaml but no AGENTS.md/CLAUDE.md/GEMINI.md.
    # Assert .git-clone-status contains template_path_invalid.

def test_startup_does_not_use_eval_for_clone_commands():
    # script must not contain eval "${CLONE_CMD}" or eval "${SHALLOW_CLONE_CMD}".
```

The harness may set `HOME`/workspace paths by patching a temporary copy of the script from `/home/developer` to a temp workspace, or by running in a controlled container-like temp tree if simpler. The important requirement is that the copy/validation block is actually executed.

- [ ] **Step 2: Implement startup support**

In `startup.sh`:

- remove `eval` from clone execution for clone commands touched by this feature
- for shallow clone path, after clone to `/tmp/repo-clone`, choose source dir:

```bash
TEMPLATE_SOURCE_DIR="/tmp/repo-clone"
if [ -n "${GITHUB_TEMPLATE_PATH}" ]; then
  TEMPLATE_SOURCE_DIR="/tmp/repo-clone/${GITHUB_TEMPLATE_PATH}"
  # verify directory, template.yaml, and supported instruction file
fi
```

- copy with robust dotfile handling, for example:

```bash
shopt -s dotglob nullglob
for item in "${TEMPLATE_SOURCE_DIR}"/*; do
  base="$(basename "$item")"
  [ "$base" = ".git" ] && continue
  cp -a "$item" /home/developer/
done
shopt -u dotglob nullglob
```

- write `.git-clone-status` with `template_path_missing` or `template_path_invalid` on invalid subdirectories.
- verify at runtime that the selected subdirectory contains `template.yaml` and at least one supported instruction file: `AGENTS.md`, `CLAUDE.md`, or `GEMINI.md`.

- [ ] **Step 3: Run startup tests**

Run:

```bash
PYTHONPATH=src/backend python -m pytest tests/unit/test_startup_github_template_path.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit startup support**

Run:

```bash
git add docker/base-image/startup.sh tests/unit/test_startup_github_template_path.py
git commit -m "feat: copy GitHub template subdirectories at startup"
```

## Task 6: Frontend Validation

**Files:**
- Create: `/Users/yalou/src/trinity/src/frontend/src/utils/githubTemplateRefs.js`
- Modify: `/Users/yalou/src/trinity/src/frontend/src/components/CreateAgentModal.vue`
- Modify: `/Users/yalou/src/trinity/src/frontend/src/views/Settings.vue`

- [ ] **Step 1: Add frontend parser utility**

Create `src/frontend/src/utils/githubTemplateRefs.js` mirroring backend validation enough for UI:

```javascript
const REPO_PATTERN = /^[a-zA-Z0-9._-]+\/[a-zA-Z0-9._-]+$/
const BRANCH_PATTERN = /^[a-zA-Z0-9._/-]{1,128}$/

function validateBranch(branch) {
  if (!branch || !BRANCH_PATTERN.test(branch)) throw new Error('Invalid branch')
  if (branch.includes('..') || branch.includes('//') || branch.startsWith('/') || branch.endsWith('/') || branch.includes('@{') || branch.includes('\\')) {
    throw new Error('Invalid branch')
  }
  if (branch.split('/').some(part => part.endsWith('.lock'))) {
    throw new Error('Invalid branch')
  }
  return branch
}

function validatePath(path) {
  if (!path || path.startsWith('/') || path.includes('\\') || path.includes('@')) throw new Error('Invalid template path')
  const parts = path.split('/')
  if (parts.some(part => !part || part === '.' || part === '..' || /\s/.test(part))) throw new Error('Invalid template path')
  return parts.join('/')
}

export function parseGithubTemplateRef(input) {
  let value = String(input || '').trim()
  const urlMatch = value.match(/github\.com\/([^/\s#?.]+\/[^/\s#?.]+)(.*)?$/)
  if (urlMatch) value = `${urlMatch[1].replace(/\.git$/, '')}${urlMatch[2] || ''}`
  if (value.startsWith('github:')) value = value.slice('github:'.length)
  let branch = null
  if (value.includes('@')) {
    const idx = value.lastIndexOf('@')
    branch = validateBranch(value.slice(idx + 1))
    value = value.slice(0, idx)
  }
  let repo = value
  let templatePath = null
  if (value.includes('//')) {
    const parts = value.split('//')
    if (parts.length !== 2) throw new Error('Invalid template ref')
    repo = parts[0]
    templatePath = validatePath(parts[1])
  }
  if (!REPO_PATTERN.test(repo)) throw new Error('Invalid repository')
  let canonical = repo
  if (templatePath) canonical += `//${templatePath}`
  if (branch) canonical += `@${branch}`
  return { canonical, repo, templatePath, branch, templateId: `github:${canonical}` }
}
```

- [ ] **Step 2: Update CreateAgentModal**

Replace `parseGithubRepo` with `parseGithubTemplateRef` and return `parsed.canonical` for custom GitHub template inputs.

- [ ] **Step 3: Update Settings**

Use `parseGithubTemplateRef` in `addGithubTemplate()`. Store canonical refs and dedupe canonical refs.

Update validation message to mention:

```text
owner/repo, owner/repo@branch, owner/repo//path, or owner/repo//path@branch
```

- [ ] **Step 4: Run frontend build or targeted checks**

Run:

```bash
npm --prefix src/frontend run build
```

Expected: build succeeds.

- [ ] **Step 5: Commit frontend support**

Run:

```bash
git add src/frontend/src/utils/githubTemplateRefs.js src/frontend/src/components/CreateAgentModal.vue src/frontend/src/views/Settings.vue
git commit -m "feat: accept subdirectory GitHub template refs in UI"
```

## Task 7: Restructure Template Repository

**Files:**
- Move files inside `/Users/yalou/src/trinity-agent-templates`

- [ ] **Step 1: Move current template into `research-agent/`**

In `/Users/yalou/src/trinity-agent-templates`, create `research-agent/` and move current template files into it:

```bash
mkdir -p research-agent
git mv template.yaml AGENTS.md CLAUDE.md GEMINI.md README.md .gitignore requirements.txt reports tests research-agent/
```

- [ ] **Step 2: Add root README**

Create `/Users/yalou/src/trinity-agent-templates/README.md`:

```markdown
# Trinity Agent Templates

Multi-template repository for Trinity GitHub agent templates.

## Templates

### Research Agent

Use this template in Trinity as:

```text
sunyalou/trinity-agent-templates//research-agent
```

Or with the explicit GitHub template prefix:

```text
github:sunyalou/trinity-agent-templates//research-agent
```

## Layout

Each template lives in its own subdirectory and must contain its own `template.yaml` plus runtime instruction files.
```

- [ ] **Step 3: Validate moved template**

Run:

```bash
cd research-agent && python tests/validate_template.py
```

Expected: `Template validation passed`.

- [ ] **Step 4: Commit template repo restructure**

Run in `/Users/yalou/src/trinity-agent-templates`:

```bash
git add -A
git commit -m "chore: organize templates into subdirectories"
```

## Task 8: Final Verification

**Files:**
- Verify Trinity repo and template repo.

- [ ] **Step 1: Run backend unit tests**

Run:

```bash
PYTHONPATH=src/backend python -m pytest tests/unit/test_github_template_ref.py tests/unit/test_github_subdirectory_templates.py tests/unit/test_startup_github_template_path.py -q
```

Expected: PASS.

- [ ] **Step 2: Run frontend build**

Run:

```bash
npm --prefix src/frontend run build
```

Expected: PASS.

- [ ] **Step 3: Validate template subdirectory**

Run:

```bash
cd /Users/yalou/src/trinity-agent-templates/research-agent && python tests/validate_template.py
```

Expected: `Template validation passed`.

- [ ] **Step 4: Check git statuses**

Run:

```bash
git -C /Users/yalou/src/trinity status --short
git -C /Users/yalou/src/trinity-agent-templates status --short
```

Expected: both clean.

## Plan Self-Review

- Spec coverage: Tasks cover parser, metadata/cache, settings, agent creation/env/git-sync behavior, startup copy behavior, frontend validation, template repo restructure, and final verification.
- Placeholder scan: `owner/repo`, `path`, and branch examples are intentional user-facing syntax examples, not missing implementation details.
- Type consistency: Parser object fields are consistently `canonical`, `repo`, `template_path`, `branch`, and `template_id`; API fields distinguish user-facing `github_repo` from cloneable `repo`/`clone_repo`.
