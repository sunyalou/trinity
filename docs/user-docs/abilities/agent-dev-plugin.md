# agent-dev Plugin

Development tools for extending existing agents with skills, memory systems, and workflows.

## Installation

```bash
/plugin install agent-dev@abilityai
```

## Skills

| Skill | Description |
|-------|-------------|
| `/agent-dev:create-playbook` | Create a new skill/playbook for the agent |
| `/agent-dev:adjust-playbook` | Modify an existing skill/playbook |
| `/agent-dev:add-memory` | Add memory system (file-index, brain, json-state, workspace) |
| `/agent-dev:add-backlog` | Add GitHub Issues backlog workflow |
| `/agent-dev:add-git-sync` | Add git-as-state hooks (auto-commit on tool use) |
| `/agent-dev:backlog` | View GitHub Issues backlog |
| `/agent-dev:claim` | Claim the next issue from the backlog |
| `/agent-dev:close` | Close current issue without a commit |
| `/agent-dev:commit` | Commit changed skill files and close the in-progress issue |
| `/agent-dev:autoplan` | Analyze an issue before implementing — research and planning phase |
| `/agent-dev:groom` | Groom the backlog — tag untagged issues with size/priority |
| `/agent-dev:roadmap` | Strategic view of the backlog — open issues grouped by milestone |
| `/agent-dev:sprint` | Human-supervised development cycle |
| `/agent-dev:work-loop` | Autonomous work loop — process backlog issues until empty |
| `/agent-dev:plan` | Plan and execute large multi-session projects |

## Memory Systems

Add persistent memory to agents via `/agent-dev:add-memory`:

| System | Purpose | Use When |
|--------|---------|----------|
| **file-index** | Workspace file awareness and search | Agent needs to know what files exist |
| **brain** | Zettelkasten-style knowledge graph | Agent builds connected notes over time |
| **json-state** | Structured JSON state with jq updates | Agent tracks counters, config, or structured data |
| **workspace** | Multi-session project tracking | Agent works on long-running projects |

Memory systems are copied directly into the agent — no plugin dependency at runtime.

## Playbook Development

### Create a New Playbook

```bash
/agent-dev:create-playbook
```

Guides through:
1. **Purpose** — What the playbook accomplishes
2. **Triggers** — When it should run
3. **Steps** — The workflow steps
4. **State** — What data it reads/writes

### Modify an Existing Playbook

```bash
/agent-dev:adjust-playbook daily-report
```

Options:
- Add/remove steps
- Change automation level
- Update schedule
- Fix issues

## GitHub Backlog Workflow

Add task management via GitHub Issues:

```bash
/agent-dev:add-backlog
```

This installs:
- `/backlog` — View current issues
- `/claim` — Claim next task to work on
- `/close` — Mark current task done (without commit)
- `/commit` — Commit skill changes and close the issue
- `/groom` — Tag and prioritize untagged issues
- `/roadmap` — View issues grouped by milestone

### Autonomous Work Loop

Run the agent autonomously through its backlog:

```bash
/agent-dev:work-loop
```

The agent:
1. Claims the highest priority issue
2. Works on it until complete
3. Commits and closes the issue
4. Claims the next one
5. Repeats until backlog is empty or time limit reached

### Human-Supervised Sprint

For sessions where you want to stay in the loop:

```bash
/agent-dev:sprint
```

Prompts for approval at key decision points rather than running autonomously.

## Multi-Session Planning

For large projects that span multiple sessions:

```bash
/agent-dev:plan
```

Creates a persistent plan that tracks:
- Overall goals and milestones
- Current session focus
- Completed work
- Next steps

## Git Sync

Add automatic git commits on every tool use:

```bash
/agent-dev:add-git-sync
```

Installs hooks that commit the agent's state after each tool call — useful for agents that modify their own skills or knowledge base.

## See Also

- [create-agent Plugin](create-agent-plugin.md) — Create new agents
- [Skills and Playbooks](../automation/skills-and-playbooks.md) — How skills work in Trinity
- [Abilities Overview](overview.md) — Full toolkit overview
