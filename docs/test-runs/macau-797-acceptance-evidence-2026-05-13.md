# Macau (#797 / issue-678) — Reviewer Acceptance Criteria Evidence

**Date:** 2026-05-13 BST
**Branch:** `AndriiPasternak31/issue-678-plan` (rebased onto current `origin/dev`)
**Live backend:** `trinity-backend` container (port 8000), `/data/trinity.db`

## Why a focused script, not e2e reader-race reproduction

The reader-race is a non-deterministic timing bug — a tool subprocess (or
MCP grandchild) inherits claude's stdout fd and wedges the reader thread
between claude exit and the result-line being captured. Reproducing this
on demand on staging is impractical (the very reason #678 needed a
recovery pipeline in the first place).

What we CAN deterministically validate is that **the backend's recovery
pipeline does the right thing when the reader-race signature arrives**.
The script below mocks only `agent_post_with_retry` to return the exact
structured 502 body that the new `error_classifier._classify_empty_result`
emits. Everything else — `TaskExecutionService.execute_task`,
`capacity_manager`, `activity_service`, the auto-retry gate, the cost
accumulator, `update_execution_status`, the live SQLite DB write — runs
unmodified against the actual production code on the live `trinity.db`.

This proves both acceptance criteria with real DB rows queryable after
the run.

## Acceptance #2 — `retry_count=1` when retry succeeds

**Setup**: mocked agent returns `[reader-race 502, success 200]`.

**Live output**:

```
[TaskExecService] Reader-race signature on trinity-system
  (num_turns=2, prev_cost=$0.0500) — auto-retry 1/1
```

**DB row**:

```
Execution ID:    6AmUgSbpF-dMU0kL-xRH2A
Result status:   TaskExecutionStatus.SUCCESS
Result cost:     0.08
DB row status:   success
DB row retry:    1
DB row cost:     0.08
Expected:        status=success, retry_count=1, cost=0.08
VERDICT: PASS
```

`cost=0.08 = 0.05 (failed first attempt, rolled in via
previous_attempt_cost) + 0.03 (retry success)`. **`retry_count=1`** is
the exact value the reviewer asked for.

## Acceptance #1 — Salvage cost + context, not null, on double-failure

**Setup**: mocked agent returns `[reader-race 502, reader-race 502]` —
the auto-retry fires but also returns the reader-race body. This is the
worst case where the failure row would have been null-everything before
#678.

**Live output**:

```
[TaskExecService] Reader-race signature on trinity-system
  (num_turns=2, prev_cost=$0.0500) — auto-retry 1/1
[TaskExecService] Failed to execute task on trinity-system:
  Execution completed without a result message after 0 tool calls /
  2 turns (raw_messages=0 types=<none>, parse_failures=0).
```

**DB row**:

```
Execution ID:    Q06wdBqFtPegQlGzqIVvWw
Result status:   TaskExecutionStatus.FAILED
Result cost:     0.1
Result ctx_used: 100
DB row status:   failed
DB row retry:    1
DB row cost:     0.1
DB row ctx_used: 100
Expected:        status=failed, retry_count=1, cost>0 (salvaged + rolled-in), context_used>0
VERDICT: PASS
```

Before #678 this row would have been:
- `cost = NULL`
- `context_used = NULL`
- `retry_count` column wouldn't exist

After #678:
- `cost = 0.10` ($0.05 from the failed first attempt + $0.05 salvaged
  from the failed retry's dict body — total burn, not null)
- `context_used = 100` (cache_read_tokens, salvaged from the 502 body's
  partial metadata)
- `retry_count = 1` (auto-retry fired)
- The agent-server side also sets `metadata.recovered_from_jsonl=True`
  before sanitization — confirmed by reading
  `_recover_metadata_from_jsonl` and the new unit tests
  (`test_jsonl_metadata_recovery.py`, 24 tests pass).

## Supporting unit-test coverage

The four #678 unit test files all pass against the rebased code:

```text
$ pytest unit/test_jsonl_metadata_recovery.py \
         unit/test_auto_retry_reader_race.py \
         unit/test_empty_result_classification.py \
         unit/test_error_classifier_dict_body.py -v
63 passed, 6 warnings in 0.34s
```

Coverage spans:

| Test file | Lines / Tests | What it covers |
|---|---:|---|
| `test_jsonl_metadata_recovery.py` | 474 / 24 | JSONL salvage path; cost/duration/num_turns/per-call usage back-fill; **12 parametrized hostile session_id shapes** (path-traversal defense); truncation; ISO-Z + offset timestamp parsing; per-call vs cumulative usage invariant; empty / pre-turn / no-result cases |
| `test_auto_retry_reader_race.py` | 156 / 14 | `_is_reader_race_signature` gating — positive shape, zero-turns, threshold boundary, num_turns >= 5 blocked, parse_failures blocked, recovery_attempted=False blocked, non-race message blocked, missing metadata blocked, string/None/list detail rejected |
| `test_empty_result_classification.py` | 72 / 7 | dict-body shape from `_classify_empty_result`; metadata serialization; sanitize_text + sanitize_dict applied; recovery_attempted flag set |
| `test_error_classifier_dict_body.py` | 143 / 18 | Body shape across all classifier branches; sanitization invariants; field types |

## Reproducing this run

The script lives at `/tmp/staging-acceptance.py` and is copied into the
backend container at `/app/staging-acceptance.py`. Run:

```bash
docker exec trinity-backend python /app/staging-acceptance.py
```

The two created rows persist in `/data/trinity.db` with the IDs above
(`6AmUgSbpF-dMU0kL-xRH2A`, `Q06wdBqFtPegQlGzqIVvWw`); query directly via:

```bash
docker exec trinity-backend python -c "
import sqlite3
conn = sqlite3.connect('/data/trinity.db')
cur = conn.cursor()
cur.execute('''SELECT id, status, retry_count, cost, context_used
              FROM schedule_executions
              WHERE id IN ('6AmUgSbpF-dMU0kL-xRH2A','Q06wdBqFtPegQlGzqIVvWw')''')
for row in cur.fetchall():
    print(row)
"
```

## Conclusion

Both acceptance criteria from the reviewer comment are demonstrably met
against the live backend code path with real DB writes. Mocking is
scoped to a single function (`agent_post_with_retry`) so the full
recovery pipeline — gate → audit log → retry → cost rollup → salvage on
failure → DB write — runs end-to-end without modification.

The reader-race itself is non-deterministic and not part of the test
plan's reproducibility contract; what IS testable is the recovery
pipeline that fires once the signature arrives, and that is fully
verified here.
