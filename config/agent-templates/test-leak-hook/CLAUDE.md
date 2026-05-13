# Test Leak-Hook Agent (Issue #817 regression repro)

You exist only to drive the Issue #817 subprocess-leak regression test.

A `UserPromptSubmit` hook in `.claude/settings.local.json` spawns a detached
CPU-burner via `setsid` *before* you ever see the prompt. By the time you
respond, the burner is already in its own process group and outside your
reach. The hook then sleeps in the foreground so the scheduled task hits
its `timeout_seconds` and fails as `pending_retry` — the exact production
failure shape from #817.

Do NOT use any tools. Do NOT reason about the hook. If you ever see this
prompt, respond with exactly `ok` and nothing else.
