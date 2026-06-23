# GitHub Research Agent Template Design

Date: 2026-06-23
Status: Draft for user review

## Goal

Create a reusable GitHub-hosted Trinity agent template for a general research/reporting agent. The template should be implemented in the existing template repository at `/Users/yalou/src/trinity-agent-templates` and be usable from Trinity's GitHub template flow by adding the corresponding GitHub repository to Settings → GitHub Templates, or by creating an agent directly with `github:owner/repo[@branch]`.

The first version should be deliberately simple: a small, portable template repository with clear agent instructions, metadata, report output conventions, and no required external credentials.

Trinity's GitHub template loader expects `template.yaml` and instruction files at the GitHub repository root. If `/Users/yalou/src/trinity-agent-templates` is a multi-template repository, this research template should live in a subdirectory during local authoring but must be published/exported so that the research template directory becomes the root of the GitHub repo referenced by Trinity.

## Non-Goals

- Do not change Trinity platform code.
- Do not require MCP servers or API credentials in v1.
- Do not create a family of specialized templates yet.
- Do not embed secrets, real tokens, or environment-specific configuration.
- Do not assume the GitHub repository already exists; implementation should produce files that can be copied into a new repository.

## Template Repository Shape

Recommended repository name:

```text
trinity-agent-researcher
```

Repository contents:

```text
trinity-agent-researcher/
  template.yaml
  CLAUDE.md
  AGENTS.md
  GEMINI.md
  README.md
  .gitignore
  reports/.gitkeep
```

Instruction files should follow each runtime's official convention:

- Claude Code: `CLAUDE.md`
- OpenCode: `AGENTS.md`
- Gemini CLI: `GEMINI.md`

The v1 template defaults to OpenCode, so `AGENTS.md` is the primary runtime instruction file. `CLAUDE.md` is still required because Trinity's current template compatibility checks require a non-empty `CLAUDE.md`, and it also supports Claude Code users. `GEMINI.md` supports users who later switch the template/runtime to Gemini CLI. To avoid divergent behavior, all three files should contain equivalent instructions, adapted only where runtime-specific wording is necessary.

### `template.yaml`

`template.yaml` is the key metadata file Trinity reads through the GitHub API. It should include:

- stable `name`
- human-friendly `display_name`
- concise `description` and `tagline`
- version and author
- modest resource requirements
- runtime selection for OpenCode + DeepSeek
- capabilities and use cases for UI preview
- no MCP servers in v1
- no required credentials in v1
- basic metrics definitions

Runtime defaults:

```yaml
runtime:
  type: opencode
  model: deepseek-openai/deepseek-v4-flash
  permission: standard
```

The runtime fields use the template schema read by Trinity's agent creation path: `runtime.type`, `runtime.model`, and `runtime.permission`. The `deepseek-openai/deepseek-v4-flash` value will only work on Trinity installations where that provider/model is configured.

Canonical `template.yaml` for v1:

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

### Runtime instruction files

The template should include runtime-specific instruction files instead of assuming one universal canonical file:

- `AGENTS.md` for OpenCode. This is the primary instruction file for the default v1 runtime.
- `CLAUDE.md` for Claude Code and Trinity template compatibility validation.
- `GEMINI.md` for Gemini CLI.

Each file should define the same agent behavior. Do not make `AGENTS.md` or `GEMINI.md` merely point to `CLAUDE.md`; each runtime should find usable instructions at its official default path.

The instructions should define:

- the agent's role as a general research/reporting assistant
- expected workflows for `/research`, `/brief`, `/compare`, and `/status`
- default report structure
- file output convention under `reports/`
- source-handling expectations
- safety rules: no secret exposure, no invented citations, note uncertainty explicitly

The agent should prefer grounded, structured answers and save durable reports as Markdown files.

### `README.md`

The README should explain how to use the repository as a Trinity GitHub template:

1. Create/push the repo to GitHub.
2. Configure GitHub PAT in Trinity if needed.
3. Add `owner/repo` in Settings → GitHub Templates, or use `github:owner/repo` directly.
4. Create an agent from the Templates page.

It should also document the expected commands and output paths.

### `.gitignore`

The `.gitignore` should avoid accidentally committing runtime output and credentials:

```gitignore
.env
*.key
*.pem
.credentials*
memory/
outputs/
metrics.json
reports/*.tmp.md
```

Keep `reports/.gitkeep` tracked so the reports directory exists in new agents.

## Agent Behavior

The slash-style commands below are conversational conventions documented in `AGENTS.md`, `CLAUDE.md`, and `GEMINI.md`. No `.claude/commands/` command files are required in v1.

The template should establish these default commands:

### `/research <topic>`

Produce a complete Markdown research report. The report should include:

- Executive Summary
- Key Findings
- Background
- Current Landscape
- Opportunities
- Risks / Unknowns
- Recommendations
- Sources / Notes

The agent should save the report as:

```text
reports/YYYY-MM-DD-topic-slug.md
```

### `/brief <topic>`

Produce a shorter brief suitable for quick decision-making. It may be returned in chat and optionally saved if the user asks.

### `/compare <A> vs <B>`

Compare two options, products, technologies, markets, or strategies. The output should include a comparison table and a recommendation section.

### `/status`

Summarize current workspace state, recent reports, and pending work.

## Data and File Flow

1. Trinity creates an agent from `github:owner/repo`.
2. The agent container clones/copies the template into its workspace.
3. Runtime instructions are read from the root instruction file for the selected runtime: OpenCode reads `AGENTS.md`, Claude Code reads `CLAUDE.md`, and Gemini CLI reads `GEMINI.md` by default.
4. User invokes research commands through Trinity chat.
5. The agent writes durable reports under `reports/`.

No platform database migration, backend change, or frontend change is required.

## GitHub Template Flow Requirements

- `template.yaml`, `AGENTS.md`, `CLAUDE.md`, and `GEMINI.md` must be at the repository root.
- Public repositories can be referenced as `github:owner/repo`, subject to GitHub API/network availability.
- Private repositories require a Trinity-configured GitHub PAT with read access.
- Branch-specific creation should use `github:owner/repo@branch`.
- If the repo is added to Settings → GitHub Templates, Trinity reads root `template.yaml` metadata through the GitHub API and caches it for about 10 minutes.

## Error Handling and Safety

- If information is uncertain or unavailable, the agent should say so explicitly.
- If live web/search tools are unavailable, the agent should use only model knowledge, user-provided context, and files present in the workspace, and label the report as not live-web verified.
- If sources are unavailable, the report should include a `Sources / Notes` section describing how the answer was derived.
- The agent must not fabricate exact URLs, citations, credentials, or private data.
- The agent should cite exact URLs only when they are present in user-provided material, workspace files, or tool output available to the runtime.
- The template must not include real secrets.
- The template should not require MCP tools in v1, so it can run on a clean Trinity deployment.

## Testing and Validation

Implementation should validate:

1. The repository skeleton contains all intended files.
2. `template.yaml` parses as YAML.
3. `template.yaml` contains required Trinity metadata fields.
4. The runtime block uses valid Trinity runtime values.
5. The files contain no obvious secret placeholders with real values.
6. The template can be referenced as `github:owner/repo` once pushed.
7. The template includes `AGENTS.md`, `CLAUDE.md`, and `GEMINI.md` at the repository root, with equivalent instructions for each runtime.
8. After publishing, create a test agent from the repo and confirm `/status` follows the template instructions.

Generated `reports/*.md` files are intended to persist in the agent workspace. They are not ignored by default so operators can choose to sync final reports back to GitHub when desired. Temporary report drafts should use `*.tmp.md` and are ignored.

If creating the repository locally first, run a YAML parse check before publishing.

## Future Extensions

After v1 works, consider separate templates or branches for:

- market research
- technical research
- investment due diligence
- competitor monitoring
- scheduled weekly briefings
- MCP-enabled web/search/browser workflows

These should be added only after the general template has been validated in normal use.
