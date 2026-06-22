# OpenCode Runtime Support

Trinity supports OpenCode as an agent runtime alongside Claude Code and Gemini CLI.

Use `runtime: opencode` in templates or choose OpenCode in the create-agent UI.

Example template runtime block:

```yaml
runtime:
  type: opencode
  model: anthropic/claude-sonnet-4-5
  permission: restricted
```

Permission profiles:

- `restricted`: read/web analysis by default; edit and bash denied.
- `standard`: normal development operations allowed; destructive commands denied or ask-based.
- `dangerous`: passes OpenCode's dangerous permission bypass flag.

OpenCode models use provider/model format, for example `anthropic/claude-sonnet-4-5`, `openai/gpt-5`, or `google/gemini-2.5-pro`.
