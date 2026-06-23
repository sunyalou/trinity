# GitHub Research Agent Template Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reusable GitHub-hosted Trinity research/reporting agent template in `/Users/yalou/src/trinity-agent-templates`.

**Architecture:** The template repository root will contain Trinity-required metadata plus runtime-specific instruction files. `template.yaml` provides Trinity UI/runtime metadata, while `AGENTS.md`, `CLAUDE.md`, and `GEMINI.md` each provide equivalent instructions at the official default path for OpenCode, Claude Code, and Gemini CLI respectively.

**Tech Stack:** Markdown, YAML, Python standard library validation script, Trinity GitHub template conventions.

---

## File Structure

Implementation target repository:

```text
/Users/yalou/src/trinity-agent-templates/
  template.yaml
  AGENTS.md
  CLAUDE.md
  GEMINI.md
  README.md
  .gitignore
  reports/.gitkeep
  tests/validate_template.py
```

Responsibilities:

- `template.yaml`: Trinity template metadata and default OpenCode runtime configuration.
- `AGENTS.md`: OpenCode instruction file and primary v1 runtime instructions.
- `CLAUDE.md`: Claude Code instruction file and Trinity compatibility requirement.
- `GEMINI.md`: Gemini CLI instruction file.
- `README.md`: Human usage documentation for adding the repo to Trinity.
- `.gitignore`: Ignore runtime outputs and secrets while keeping final reports commit-eligible.
- `reports/.gitkeep`: Keeps report output directory present in the template.
- `tests/validate_template.py`: Repository-local validation that checks YAML shape, required root files, runtime schema, secret hygiene, and instruction parity markers.

## Task 1: Add Validation Harness

**Files:**
- Create: `/Users/yalou/src/trinity-agent-templates/tests/validate_template.py`

- [ ] **Step 1: Create the validation test file**

Create `/Users/yalou/src/trinity-agent-templates/tests/validate_template.py` with this content:

```python
#!/usr/bin/env python3
"""Validate the Trinity research agent template repository."""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - explicit operator guidance
    print("PyYAML is required. Install with: python -m pip install pyyaml", file=sys.stderr)
    raise


ROOT = Path(__file__).resolve().parents[1]

REQUIRED_ROOT_FILES = [
    "template.yaml",
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "README.md",
    ".gitignore",
    "reports/.gitkeep",
]

REQUIRED_TEMPLATE_KEYS = {
    "name",
    "display_name",
    "description",
    "tagline",
    "version",
    "author",
    "resources",
    "runtime",
    "capabilities",
    "use_cases",
    "commands",
    "mcp_servers",
    "required_credentials",
    "credentials",
    "metrics",
}

REQUIRED_COMMANDS = {"research", "brief", "compare", "status"}
REQUIRED_CAPABILITIES = {
    "research-planning",
    "summarization",
    "comparison-analysis",
    "report-writing",
}
REQUIRED_INSTRUCTION_MARKERS = [
    "/research <topic>",
    "/brief <topic>",
    "/compare <A> vs <B>",
    "/status",
    "not live-web verified",
    "reports/YYYY-MM-DD-topic-slug.md",
]
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
]


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def read_text(relative_path: str) -> str:
    path = ROOT / relative_path
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        fail(f"{relative_path} is not valid UTF-8: {exc}")


def load_template() -> dict:
    path = ROOT / "template.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        fail(f"template.yaml is invalid YAML: {exc}")
    if not isinstance(data, dict):
        fail("template.yaml must parse to a mapping")
    return data


def validate_required_files() -> None:
    for relative_path in REQUIRED_ROOT_FILES:
        path = ROOT / relative_path
        if not path.exists():
            fail(f"missing required file: {relative_path}")
        if path.is_file() and relative_path != "reports/.gitkeep":
            text = read_text(relative_path)
            if not text.strip():
                fail(f"required file is empty: {relative_path}")


def validate_template_yaml(data: dict) -> None:
    missing = sorted(REQUIRED_TEMPLATE_KEYS - set(data))
    if missing:
        fail(f"template.yaml missing required keys: {', '.join(missing)}")

    if data["name"] != "trinity-agent-researcher":
        fail("template.yaml name must be trinity-agent-researcher")

    resources = data.get("resources")
    if resources != {"cpu": "1", "memory": "2g"}:
        fail("resources must be exactly {'cpu': '1', 'memory': '2g'}")

    runtime = data.get("runtime")
    expected_runtime = {
        "type": "opencode",
        "model": "deepseek-openai/deepseek-v4-flash",
        "permission": "standard",
    }
    if runtime != expected_runtime:
        fail(f"runtime must be exactly {expected_runtime!r}")

    capabilities = set(data.get("capabilities") or [])
    missing_capabilities = sorted(REQUIRED_CAPABILITIES - capabilities)
    if missing_capabilities:
        fail(f"missing capabilities: {', '.join(missing_capabilities)}")

    commands = data.get("commands") or []
    command_names = {item.get("name") for item in commands if isinstance(item, dict)}
    missing_commands = sorted(REQUIRED_COMMANDS - command_names)
    if missing_commands:
        fail(f"missing commands: {', '.join(missing_commands)}")

    if data.get("mcp_servers") != []:
        fail("v1 template must not require MCP servers")
    if data.get("required_credentials") != []:
        fail("v1 template must not require credentials")
    if data.get("credentials") != {}:
        fail("v1 template credentials must be an empty mapping")

    metric_names = {item.get("name") for item in data.get("metrics") or [] if isinstance(item, dict)}
    for metric_name in {"reports_created", "briefs_created", "comparisons_created", "research_status"}:
        if metric_name not in metric_names:
            fail(f"missing metric: {metric_name}")


def validate_instruction_files() -> None:
    for relative_path in ["AGENTS.md", "CLAUDE.md", "GEMINI.md"]:
        text = read_text(relative_path)
        for marker in REQUIRED_INSTRUCTION_MARKERS:
            if marker not in text:
                fail(f"{relative_path} missing instruction marker: {marker}")


def validate_secret_hygiene() -> None:
    for path in ROOT.rglob("*"):
        if ".git" in path.parts or path.is_dir():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                fail(f"possible secret found in {path.relative_to(ROOT)}")


def main() -> None:
    validate_required_files()
    data = load_template()
    validate_template_yaml(data)
    validate_instruction_files()
    validate_secret_hygiene()
    print("Template validation passed")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run validation to verify it fails before files exist**

Run:

```bash
python tests/validate_template.py
```

Expected: FAIL with `missing required file: template.yaml`.

- [ ] **Step 3: Commit validation harness**

Run:

```bash
git add tests/validate_template.py
git commit -m "test: add template validation harness"
```

Expected: first commit in `/Users/yalou/src/trinity-agent-templates` succeeds.

## Task 2: Add Trinity Template Metadata

**Files:**
- Create: `/Users/yalou/src/trinity-agent-templates/template.yaml`
- Create: `/Users/yalou/src/trinity-agent-templates/reports/.gitkeep`

- [ ] **Step 1: Create `template.yaml`**

Create `/Users/yalou/src/trinity-agent-templates/template.yaml` with this content:

```yaml
name: trinity-agent-researcher
display_name: Research Agent
description: General-purpose Trinity research agent for briefs, comparisons, and structured Markdown reports.
tagline: Researches topics and writes structured reports
version: "1.0.0"
author: yalou

resources:
  cpu: "1"
  memory: "2g"

runtime:
  type: opencode
  model: deepseek-openai/deepseek-v4-flash
  permission: standard

capabilities:
  - research-planning
  - summarization
  - comparison-analysis
  - report-writing

use_cases:
  - "/research <topic> - Produce a structured research report"
  - "/brief <topic> - Produce a concise decision brief"
  - "/compare <A> vs <B> - Compare two options and recommend a path"
  - "/status - Summarize current workspace state and recent reports"

commands:
  - name: research
    description: Produce a structured Markdown research report for a topic
  - name: brief
    description: Produce a concise decision brief for a topic
  - name: compare
    description: Compare two options and recommend a path
  - name: status
    description: Summarize current workspace state and recent reports

shared_folders:
  expose: true
  consume: false

mcp_servers: []

required_credentials: []

credentials: {}

metrics:
  - name: reports_created
    type: counter
    label: "Reports"
    description: "Total research reports created"
  - name: briefs_created
    type: counter
    label: "Briefs"
    description: "Total concise briefs created"
  - name: comparisons_created
    type: counter
    label: "Comparisons"
    description: "Total comparisons created"
  - name: research_status
    type: status
    label: "Status"
    description: "Current research workflow status"
    values:
      - value: "idle"
        color: "gray"
        label: "Idle"
      - value: "researching"
        color: "blue"
        label: "Researching"
      - value: "writing"
        color: "purple"
        label: "Writing"
      - value: "error"
        color: "red"
        label: "Error"
```

- [ ] **Step 2: Create report directory marker**

Run:

```bash
mkdir -p reports
touch reports/.gitkeep
```

Expected: `reports/.gitkeep` exists.

- [ ] **Step 3: Run validation to verify instruction files still fail**

Run:

```bash
python tests/validate_template.py
```

Expected: FAIL with `missing required file: AGENTS.md`.

- [ ] **Step 4: Commit metadata files**

Run:

```bash
git add template.yaml reports/.gitkeep
git commit -m "feat: add research template metadata"
```

Expected: commit succeeds.

## Task 3: Add Runtime-Specific Instruction Files

**Files:**
- Create: `/Users/yalou/src/trinity-agent-templates/AGENTS.md`
- Create: `/Users/yalou/src/trinity-agent-templates/CLAUDE.md`
- Create: `/Users/yalou/src/trinity-agent-templates/GEMINI.md`

- [ ] **Step 1: Create `AGENTS.md` for OpenCode**

Create `/Users/yalou/src/trinity-agent-templates/AGENTS.md` with this content:

```markdown
# Research Agent Instructions

You are a Trinity research and reporting agent running with OpenCode.

## Role

Help users research topics, compare options, and create structured Markdown reports. Prefer clear reasoning, explicit uncertainty, and durable files under `reports/`.

## Source Rules

- Use live web/search/tool output only when tools are available in the current runtime.
- If live web/search tools are unavailable, use only model knowledge, user-provided context, and files in the workspace.
- When live web/search tools are unavailable, label reports as `not live-web verified` in the Sources / Notes section.
- Do not invent exact URLs, citations, credentials, private data, or source quotes.
- Cite exact URLs only when they appear in user-provided material, workspace files, or runtime tool output.

## Commands

### /research <topic>

Create a complete research report for `<topic>` and save it to:

```text
reports/YYYY-MM-DD-topic-slug.md
```

Use this report structure:

```markdown
# <Topic> Research Report

## Executive Summary

## Key Findings

## Background

## Current Landscape

## Opportunities

## Risks / Unknowns

## Recommendations

## Sources / Notes
```

### /brief <topic>

Create a concise decision brief for `<topic>`. Use bullets and include the most important caveats. Save the brief under `reports/` if the user asks for a file.

### /compare <A> vs <B>

Compare two options, products, technologies, markets, or strategies. Include:

- a comparison table
- strengths and weaknesses
- risks and unknowns
- a recommendation

### /status

Summarize current workspace state, recent reports in `reports/`, and any pending research work.

## Writing Style

- Be concise and structured.
- Separate facts from inference.
- State confidence and uncertainty.
- Prefer Markdown headings, tables, and bullets.
- Save durable long-form work under `reports/`.

## Safety

- Do not expose secrets or credentials.
- Do not fabricate sources.
- Do not claim live verification unless live tools or user-provided current sources were actually used.
```

- [ ] **Step 2: Create `CLAUDE.md` for Claude Code**

Create `/Users/yalou/src/trinity-agent-templates/CLAUDE.md` with the same content as `AGENTS.md`, except change this line:

```markdown
You are a Trinity research and reporting agent running with Claude Code.
```

All command names, source rules, output paths, and safety rules must remain identical.

- [ ] **Step 3: Create `GEMINI.md` for Gemini CLI**

Create `/Users/yalou/src/trinity-agent-templates/GEMINI.md` with the same content as `AGENTS.md`, except change this line:

```markdown
You are a Trinity research and reporting agent running with Gemini CLI.
```

All command names, source rules, output paths, and safety rules must remain identical.

- [ ] **Step 4: Run validation to verify docs files still fail**

Run:

```bash
python tests/validate_template.py
```

Expected: FAIL with `missing required file: README.md`.

- [ ] **Step 5: Commit runtime instruction files**

Run:

```bash
git add AGENTS.md CLAUDE.md GEMINI.md
git commit -m "feat: add runtime instruction files"
```

Expected: commit succeeds.

## Task 4: Add README and Ignore Rules

**Files:**
- Create: `/Users/yalou/src/trinity-agent-templates/README.md`
- Create: `/Users/yalou/src/trinity-agent-templates/.gitignore`

- [ ] **Step 1: Create `README.md`**

Create `/Users/yalou/src/trinity-agent-templates/README.md` with this content:

```markdown
# Trinity Research Agent Template

Reusable Trinity GitHub template for a general-purpose research and reporting agent.

## What This Agent Does

- Researches user-provided topics.
- Produces structured Markdown reports.
- Writes durable reports under `reports/`.
- Creates concise briefs and comparisons.
- Works without required MCP servers or credentials in v1.

## Runtime Defaults

The default runtime metadata in `template.yaml` is:

```yaml
runtime:
  type: opencode
  model: deepseek-openai/deepseek-v4-flash
  permission: standard
```

This requires the target Trinity installation to have the `deepseek-openai` provider and `deepseek-v4-flash` model configured.

## Instruction Files

This repository includes instruction files for the supported runtimes:

- OpenCode: `AGENTS.md`
- Claude Code: `CLAUDE.md`
- Gemini CLI: `GEMINI.md`

The files contain equivalent agent behavior so the template can be adapted across runtimes.

## Commands

### `/research <topic>`

Creates a complete research report and saves it to:

```text
reports/YYYY-MM-DD-topic-slug.md
```

### `/brief <topic>`

Creates a concise decision brief.

### `/compare <A> vs <B>`

Compares two options and recommends a path.

### `/status`

Summarizes recent reports and workspace state.

## Add This Template to Trinity

1. Push this repository to GitHub.
2. In Trinity, configure a GitHub PAT if the repository is private.
3. Add the repository in Settings → GitHub Templates using `owner/repo`.
4. Open Templates and create an agent from `Research Agent`.

You can also create an agent directly with:

```text
github:owner/repo
```

For a specific branch:

```text
github:owner/repo@branch
```

## Source and Verification Rules

The agent does not require live web/search tools in v1. If live tools are unavailable, reports must be labeled `not live-web verified` and should rely only on model knowledge, user-provided context, and workspace files.

The agent must not fabricate citations, exact URLs, credentials, or private data.

## Validate Locally

Run:

```bash
python tests/validate_template.py
```

Expected output:

```text
Template validation passed
```
```

- [ ] **Step 2: Create `.gitignore`**

Create `/Users/yalou/src/trinity-agent-templates/.gitignore` with this content:

```gitignore
.env
.env.*
*.key
*.pem
.credentials*
memory/
outputs/
metrics.json
reports/*.tmp.md
__pycache__/
*.pyc
```

- [ ] **Step 3: Run validation to verify all template files pass**

Run:

```bash
python tests/validate_template.py
```

Expected: PASS with `Template validation passed`.

- [ ] **Step 4: Commit README and ignore rules**

Run:

```bash
git add README.md .gitignore
git commit -m "docs: add template usage guide"
```

Expected: commit succeeds.

## Task 5: Final Repository Verification

**Files:**
- Verify: `/Users/yalou/src/trinity-agent-templates/*`

- [ ] **Step 1: Run validation**

Run:

```bash
python tests/validate_template.py
```

Expected: PASS with `Template validation passed`.

- [ ] **Step 2: Verify git status**

Run:

```bash
git status --short
```

Expected: no output.

- [ ] **Step 3: Inspect commit history**

Run:

```bash
git log --oneline -5
```

Expected: shows the four template commits from Tasks 1-4.

- [ ] **Step 4: Document GitHub publishing next step**

Report this to the user:

```text
Template repository is ready locally at /Users/yalou/src/trinity-agent-templates.
To publish it, push this repository to GitHub and add owner/repo in Trinity Settings → GitHub Templates.
```

Do not push to GitHub unless the user explicitly asks.

## Plan Self-Review

- Spec coverage: The plan creates root `template.yaml`, runtime-specific instruction files (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`), README, `.gitignore`, report directory, validation, and local commits in `/Users/yalou/src/trinity-agent-templates`.
- Placeholder scan: The only `owner/repo`, `<topic>`, `<A>`, `<B>`, and `YYYY-MM-DD-topic-slug.md` strings are intentional user-facing examples.
- Type consistency: Runtime schema uses `runtime.type`, `runtime.model`, and `runtime.permission`, matching the reviewed spec.
