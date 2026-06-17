# Transactional Agent Executions — Design Recommendation (#1095)

| Field | Value |
|---|---|
| **Status** | Research / design — **GO/NO-GO gate**. Implementation does not start until this is reviewed and a GO is recorded. |
| **Date** | 2026-06-12 |
| **Issue** | [#1095](https://github.com/Abilityai/trinity/issues/1095) — *feat: transactional agent executions — discard workspace changes unless validated as success (research-gated)* · P1 · `theme-reliability` · `complexity-high` |
| **Branch** | `AndriiPasternak31/plan-issue-1095` (fresh off `dev`; no implementation in this phase) |
| **Scope** | **Workspace-filesystem-scoped only.** External side effects (email / Slack / git push / payment) are explicitly **out of scope** — that is effect-idempotency, [#1084](https://github.com/Abilityai/trinity/issues/1084). |
| **Supersedes** | [#27](https://github.com/Abilityai/trinity/issues/27) *Git Worktrees for Task Isolation* (CLOSED) — folded in as the **future concurrent track**, §10.2. |
| **Verdict** | **NO-GO for a broad mechanism on today's evidence; a narrow serialized-git v1 is GO *only* after the production evidence query in §0 clears the kill criterion.** See §0. |

> **How to read this doc.** §0 is the real first deliverable: an evidence gate that can — and on the sample available today does — return **NO-GO**. Everything after §0 is the design that becomes reachable *if* §0 clears on a representative production instance. The doc deliberately does **not** assume the build.

---

## 0. Evidence & GO/NO-GO gate (the real first deliverable)

The issue describes "recurring operator pain": a turn fails and leaves leftovers; the next turn starts dirty. A reliability subsystem (snapshot → run → validate → commit/rollback) is justified **only if that pain is real and frequent**. Before recommending a build, we state a **kill criterion** and measure against it.

### 0.1 Kill criterion

> **Build only if** failed/soft-failed turns that leave **harmful** workspace leftovers (changes the next turn must not inherit) occur often enough, and on large enough workspaces, that the operator cost exceeds the cost of a new transactional subsystem and its hazards (§9).
>
> **NO-GO if** harmful-leftover failures are rare, OR the failures that *could* leave leftovers are dominated by the **indeterminate** class (the writer's fate is unknowable, so you cannot safely roll back anyway — §6.1), OR workspaces are small enough that a cheap targeted cleanup beats a transaction.

This is falsifiable in both directions. The doc is required to allow the NO-GO conclusion.

### 0.2 Evidence gathered (real data, not assumption)

Source: the live `schedule_executions` table from a real instance — `trinity.db` extracted read-only from the `trinity_trinity-data` Docker volume (356 MB; mtime 2026-05-30; rows span **2026-05-15 → 2026-05-17**). This is the production schema, queried directly.

**Proxy validity — one-directional only (corrected).** `tool_calls` is written **only on the SUCCESS terminal** (`task_execution_service.py:903`, the lone call site that passes the column); every FAILED terminal (`:1088–1099`) and the auth-503 path omit it. So the proxy is sound in **one** direction — **`tool_calls` present ⇒ the turn mutated the workspace** (16/16 `success` rows populate it) — but the converse does **not** hold: **`tool_calls` empty on a failed row is a persistence artifact, not evidence of "no writes."** "0/6 failed rows populate it" below is therefore a *tautology of the write path*, not a measurement — a failed turn that wrote files records an empty `tool_calls` just the same. We lean on the *qualitative* failure classification (below) and on duration, **never** on tool_calls-emptiness, to reason about failed-turn leftovers.

| Metric | Value |
|---|---|
| Total executions sampled | **23** |
| `success` | 16 (69.6%) — avg 50 s, max 384 s; all 16 made tool calls (Bash heredocs writing `predictions.csv`, `Edit` on `.claude/skills/*/workspace/`) |
| `failed` | **6 (26.1%)** |
| `cancelled` | 1 (4.3%) — `agent_deleted`, 0 tool calls |
| Failed turns that recorded **any** workspace tool call | **0 of 6** |
| Soft-fail / validation columns populated (`validated_at`, `validation_execution_id`, `business_status`) | **0 of 23** |
| Reader-race retries in sample (`retry_count` sum, `attempt_number` max) | 0 / 1 |

**The 6 failures, classified:**

| Class | n | Examples (agent · dur · error) | Workspace-leftover risk |
|---|---|---|---|
| **Auth — caught early in the turn** | 3 | `cleon` · 1.2 s · *Not logged in*; `cornelius-oracle` · 7.1 s · *API 401*; `oracle-3-ai` · 0.9 s · *Not logged in* | **Low, not structurally zero.** The `claude` subprocess spawns **unconditionally** (`claude_code.py:280–304`; no pre-flight auth gate); auth/401 is detected **mid-stream** (`:437–448`), not before Claude starts. The sub-2 s durations make pre-write failure near-certain *in this sample*, but a mid-turn 401 (token expiry / the 7.1 s case) could in principle land after a `tool_use` already ran. |
| **Transport — backend never saw the result** | 3 | `test-event-subscriptions` · 10.1 s · `RemoteProtocolError`; `test-parallel-task` · **39.0 s** · `RemoteProtocolError`; `test-parallel-task` · 3.1 s · `ConnectError` | **Indeterminate** — `tool_calls` is empty because the result stream broke, *not* because no work happened. The 39 s turn especially could have written files the backend cannot see. |

### 0.3 What the evidence says

1. **Directly-observed harmful leftovers = 0/23 — where "observed" is itself weak.** No failure in the sample is the issue's archetype (a turn that wrote files, then failed, leaving a dirty tree the next run inherits). But because failed rows never record `tool_calls` (§0.2), "observed" here means only "no *backend-visible* leftover" — and that blindness is exactly finding #2.
2. **The only failures that *could* leave leftovers are the 3 transport failures — and those are exactly the class you *cannot* safely roll back**, because the backend has no proof the writer is dead or what it wrote (§6.1, H5). A naive "rollback on failure" would either (a) quarantine them anyway, or (b) risk discarding a turn that actually *succeeded* on the agent side but lost its result on the wire. This is the indeterminate-state problem, and it is the dominant failure class here.
3. **Soft-fail is not evidenced at all.** The validation machinery (`business_status`, `validated_at`) exists in the schema but is 100% unused in this sample — so "exit 0 but bad output" is, on this data, hypothetical.

### 0.4 Honest caveats (do not over-read the sample)

- **n = 23 is tiny** and the window is 3 days. This is a dev/test-flavored instance (agents named `test-parallel-task`, `test-event-subscriptions`); it is **not** a production fleet of long-running, file-writing scheduled jobs.
- **`tool_calls = 0` on *any* failed row ≠ "no writes."** Failed terminals never record `tool_calls` (success-only write path, §0.2), so the backend is blind on **every** failure class — transport *and* auth. Auth failures are near-zero-risk here only *empirically* (they failed in under ~7 s), not because the column proves it.
- A production instance with heavy scheduled file-mutating work could show a materially higher harmful-leftover rate. **The sample cannot rule that in or out.**

### 0.5 Verdict — and the gate to flip it

On the evidence available today: **NO-GO for a broad transactional subsystem.** The pain is unproven, and the failure class that could cause it is the one a transaction cannot safely act on.

**This NO-GO is conditional, not final.** Before any GO, run the kill-criterion query below against a **representative production instance** (≥ 30 days, real scheduled workload). The doc is a GO only if the numbers clear the criterion *and* a reviewer accepts the §6/§9 hazards.

```sql
-- Run on a production trinity.db (≥30d window). Harmful-leftover proxy =
-- a non-success terminal that recorded a workspace-mutating tool call.
WITH term AS (
  SELECT id, agent_name, status, duration_ms, error,
         CASE WHEN tool_calls IS NULL OR tool_calls IN ('','[]') THEN 0 ELSE 1 END AS wrote,
         -- did the recorded tool_calls include a filesystem mutation?
         -- NB: schedule_executions.tool_calls stores the RAW Claude stream-json
         -- (task_execution_service.py:856-862 → response_data["execution_log"]),
         -- where a tool call is {"type":"tool_use","name":"Write",...} — keyed
         -- "name", NOT "tool". The simplified "tool" key only lands in
         -- chat_messages, never here, so a '"tool": ...' LIKE matches nothing.
         -- Bash over-counts (most Bash calls don't mutate the tree); treat
         -- maybe_fs as an upper bound and spot-check the matched rows.
         CASE WHEN tool_calls LIKE '%"name": "Write"%'
               OR tool_calls LIKE '%"name": "Edit"%'
               OR tool_calls LIKE '%"name": "NotebookEdit"%'
               OR tool_calls LIKE '%"name": "Bash"%' THEN 1 ELSE 0 END AS maybe_fs
  FROM schedule_executions
  -- ISO-Z cutoff with sub-second precision to match utc_now_iso() (Invariant #16);
  -- a bare-second Z cutoff ('...:%SZ') drops boundary-second rows because '.' < 'Z'.
  WHERE started_at >= strftime('%Y-%m-%dT%H:%M:%f','now','-30 days') || 'Z'
)
SELECT
  COUNT(*)                                              AS terminals,
  SUM(status='failed')                                 AS failed,
  SUM(status='failed' AND wrote=1)                     AS failed_with_tools,        -- backend-visible leftovers
  SUM(status='failed' AND maybe_fs=1)                  AS failed_with_fs_writes,    -- the harmful-leftover proxy
  SUM(status='failed' AND wrote=0
        AND error LIKE '%RemoteProtocolError%')         AS failed_transport_indet,   -- indeterminate (can't roll back)
  ROUND(100.0*SUM(status='failed' AND maybe_fs=1)/NULLIF(SUM(status='failed'),0),1) AS harmful_pct_of_failures
FROM term;
```

> **Decision rule.** If `failed_with_fs_writes` is a small fraction of failures **and** `failed_transport_indet` dominates → **NO-GO** (cheaper to add lease re-delivery from a clean tree, §6.4, than a transaction). If `harmful_pct_of_failures` is materially high on real workspaces → proceed to the §1 narrow v1. Workspace-size sampling (below) sets the cost side of the inequality.

Workspace-size sampling (cost side of the inequality), per running agent container:

```bash
# Bytes the transaction would have to snapshot/diff, excluding hard-excludes (D1/§3).
docker exec agent-<name> du -sx --exclude=.git --exclude=node_modules \
  --exclude='*/.venv' --exclude=content --exclude=.trinity/backup /home/developer
```

---

## 1. Problem framing & v1 scope

The issue decouples two sub-problems; the doc keeps them decoupled:

1. **Transaction boundary** — snapshot → run → commit-or-discard, *workspace only*.
2. **Validation gate** — what decides "success?" Hard-fail is a typed terminal `error_code`; the painful (and here unevidenced) case is **soft-fail** (exit 0, bad output), which needs an **explicit** validator (§4).

**v1 = the single defensible quadrant (OV1):**

> **Serialized** (`max_parallel_tasks == 1`) **+ clean-baseline git** workspace **+ per-execution opt-in** (§7) **+ container-local coordinator** (§3), **gated behind §0**.

Everything else is **named but deferred** (§10): non-git/tar workspaces, concurrent worktree-per-execution (#27), external side effects (#1084).

### Relationship to the pull / work-stealing direction (`TARGET_ARCHITECTURE.md`)

This is not a side feature — it composes with the active execution-stack direction:

- `TARGET_ARCHITECTURE.md:276` already plans `~/.trinity/post-check` ("runs after every task completion, language-agnostic, shebang-selected … output validation"). **The v1 validator IS that hook** (§4).
- The pull model's recovery primitive is **lease-expiry re-delivery** (`:195`–`:203`): a dead turn's row flips back to `queued` and is re-pulled with the **same `execution_id` + idempotency key**, absorbed by a result-POST **compare-and-set** (`:274`). The workspace transaction must make a **re-pulled turn start from a clean tree** — otherwise re-delivery resumes on the previous attempt's leftovers. This is the synergy the issue's "Lease re-delivery synergy" research question asks us to confirm (§6.4).
- The agent-side coordinator is the natural home: the pull worker pool is "built on the existing in-container asyncio-loop precedent (`auto_sync.py`)" (`:268`). The transaction coordinator wraps that same worker (§3) — consistent with Invariant #8 (Trinity is not the orchestration engine; agents own multi-stage work).

---

## 2. v1 mechanism — serialized git, transaction-owned ref (T2 · D1 · OV2)

A git-native workspace already has the primitive we want: a commit is an atomic content snapshot, and a ref is a cheap pointer to one. v1 uses a **transaction-owned ref** to bound a turn's writes, instead of the destructive `reset --hard && git clean -fd` that the issue's "destructive-reset-then-overlay" precedent (`git.py:1140`) uses for a different purpose.

### 2.1 Clean-baseline precondition (H6)

Before the turn, the coordinator inspects the workspace:

- **Clean** (no staged/modified/untracked-non-ignored changes vs `HEAD`) → record `baseline = HEAD`, proceed.
- **Dirty** → **reject the transaction** for this turn (run non-transactionally, log a warning) **or** capture the dirty delta into a `tx-dirty` stash first so it is *not* attributed to — and not discarded by — the turn. v1 ships **reject-and-warn**; "snapshot-dirty-separately" is a documented follow-up.

Rationale: a transaction that rolls back must roll back **exactly the turn's writes**, never a human's in-flight terminal edit or another writer's commit (§6.2). Without a clean baseline you cannot attribute the delta, so you cannot safely discard it.

### 2.2 Boundary

```
baseline = current HEAD (clean)        # the only thing rollback returns to
run the turn → it writes into the working tree as normal
on terminal:
  COMMIT  : git add -A (within include-set, §2.4)
            git commit  →  advance the working branch to the new commit
            (commit gated on the execution-row CAS, §6.3)
  ROLLBACK: restore the working tree to `baseline` for the include-set ONLY,
            using a tree-level checkout, NOT `reset --hard`/`clean -fd`:
              git restore --source=<baseline> --staged --worktree -- <include-paths>
              git clean   -fd -- <include-paths>          # scoped to include-set, never bare
            then overlay the rollback-survives journal set (§2.3)
```

The transaction owns a **ref/index it created** (e.g. a `refs/trinity/tx/<execution_id>` pointer to `baseline`). Commit advances the working branch; rollback discards the tx ref and restores the include-set to `baseline`. The working branch never moves on rollback.

### 2.3 Rollback-survives = a **new narrow journal set** (D1 — load-bearing)

On rollback the agent must still **remember it failed** (so it does not loop, and #560 failure-memory has something to persist). The set of paths that survive rollback is a **new, narrow journal set**, e.g.:

```
.trinity/journal/**          # failed-attempt records (this turn's audit)
.trinity/failure-memory/**   # #560 persisted failure memory
```

**It is NOT the persistent-state allowlist.** The default allowlist (`files.py:229`, verified) is:

```python
_DEFAULT_PERSISTENT_STATE = ["workspace/**", ".trinity/**", ".mcp.json",
                             ".claude.json", ".claude/.credentials.json"]
```

`workspace/**` is the *work directory* — the very thing a rollback must discard. **Reusing the persistent-state allowlist as the rollback-survives set would neuter the rollback.** The two sets are deliberately separate: the allowlist answers "what survives a *reset-to-main*"; the journal set answers "what survives a *transaction rollback*." Overlap is intentional only at `.trinity/journal` / failure-memory, which must be excluded from the transaction's include-set so the rollback restore does not clobber them.

### 2.4 Snapshot scope = explicit include-set + hard excludes (D1)

The transaction acts on an **explicit include-set**, NOT "the complement of the persistent-state allowlist." Hard excludes (never snapshotted, never rolled back):

| Exclude | Why |
|---|---|
| `.git/` | The ref machinery itself; rolling it back corrupts the transaction. |
| `node_modules/`, `*/.venv`, `__pycache__`, build caches | Huge, regenerable; snapshotting them makes the transaction unaffordable (§0.5 sizing). |
| `content/` | Generated assets, gitignored by template convention. |
| `.trinity/backup/`, `.trinity/journal/`, `.trinity/failure-memory/` | Backup target + the rollback-survives journal set (§2.3). |
| **Mounted volumes under `/home/developer`** — `shared-out` (`crud.py:652`), `shared-in/{agent}` (`crud.py:663` → `/home/developer/shared-in/{source_agent}`) | **Device-boundary (H8):** these are *separate Docker volumes*, not the agent's workspace volume. A git operation that crosses into them touches another agent's data. They must be path-excluded. |

---

## 3. Coordination boundary — container-local (T4 · §3)

Snapshot → run → quiesce → validate → commit/rollback all live in **one container-local coordinator** that wraps the Claude subprocess — **not** backend `docker-exec` orchestration.

**Why container-local:**

- **No network/crash gap.** If the boundary were driven by the backend over `docker exec` (the `pre_check_service.py` pattern), a backend restart or a dropped connection *between* "run" and "commit/rollback" leaves the workspace in an undefined state with no local owner to finish the transaction. The 3 transport failures in §0.2 are exactly this gap. A coordinator co-located with the writer survives backend disconnects.
- **Fits the target direction.** The pull worker pool already runs in-container on the `auto_sync.py` asyncio precedent (`TARGET_ARCHITECTURE.md:268`). The coordinator is a wrapper around that worker's per-task body: `claim → snapshot → run Claude → post-check → commit/rollback → POST result`. Invariant #8: the agent owns the multi-stage work; Trinity provides the queue + result CAS, not a transaction executor.
- **Single terminal writer.** The coordinator is the one place that classifies the terminal and decides commit vs rollback, which is what makes the §6.3 CAS gate meaningful.

**Consequence / open question:** today the terminal is classified **backend-side** in `task_execution_service.py` (§6.3). A container-local coordinator either (a) moves terminal classification into the agent (the pull model already moves result-reporting agent-side, `:274`), or (b) keeps classification backend-side and has the coordinator *act on the backend's verdict* — which reintroduces a network round-trip inside the transaction. v1 recommends (a), aligned with pull; this is flagged in §9 (H11) as the highest-coupling decision.

---

## 4. Validation gate — `~/.trinity/post-check` (T4 · §4 · H10 · H12)

### 4.1 The hook

Mirror `pre_check_service.py` (`run_pre_check`, `:44`, verified): an executable `~/.trinity/post-check`, **shebang-selected interpreter** (Trinity prefixes none), stdout/exit contract, stdout capped at 32 KB. Semantics for the transaction:

| Post-check outcome | Decision |
|---|---|
| **absent** | **COMMIT (fail-open).** No validator configured ⇒ only *hard* failures (typed `error_code` / non-zero / timeout) roll back. Matches the issue's "default off / partial persistence is often wanted." |
| exit 0 | COMMIT (gated on §6.3 CAS). |
| exit ≠ 0 | ROLLBACK (soft-fail caught). |
| timeout | ROLLBACK (treated as hard-fail of the validator). |

> Note the `pre_check_service` subtlety to mirror correctly: it tests the hook with `-f`, not `-x`, so a present-but-non-executable hook surfaces as exit-126 rather than a silent skip. The post-check must make the same choice deliberately (recommend: treat non-executable as a **config error → fail-open + operator-queue warning**, never a silent rollback).

### 4.2 Validator hardening (H10)

The validator runs **inside the workspace the turn just wrote** — so a malicious or buggy turn could rewrite its own validator to always pass.

- **Capture the validator pre-turn.** Read `~/.trinity/post-check` (and digest it) **before** the turn runs; execute the captured copy, not whatever the turn left behind. A digest mismatch at validation time is itself a rollback trigger.
- **No-write contract.** The validator runs read-only (recommend: a read-only bind of the work tree, or run it *after* the snapshot so any writes it makes are themselves outside the committed set). A validator that mutates the workspace breaks the "validated state == committed state" equality.

### 4.3 Relationship to `ValidationService` (H12 — avoid recursion)

`ValidationService.validate_execution` (`validation_service.py:127`, verified) dispatches a **separate** `execute_task(triggered_by="validation")` turn (`:186`) — a second Claude run against the same workspace. The post-check hook (a cheap deterministic script) and `ValidationService` (a full LLM turn) are **different validators**; the doc must prevent them from composing into recursion:

- A `triggered_by="validation"` execution **must NOT itself open a workspace transaction** (§7 excludes internal executions). Otherwise validating turn T opens a transaction that runs a validation turn that opens a transaction…
- Recommendation: the post-check hook is the **v1 validator**; `ValidationService` stays an orthogonal, opt-in, backend-side quality gate that reads `business_status` and never participates in the transaction boundary. Wiring `ValidationService` *as* the transaction validator is explicitly a **future** question, not v1.

---

## 5. Transaction lifecycle (belongs in the doc and in the coordinator's code comments)

```
                 dirty? ── yes ──> REJECT tx (run non-transactionally; warn)   [§2.1]
                   │ no
  baseline = HEAD ─┤
                   ▼
              [ RUN turn ]   (other writers quiesced or path-excluded — §6.2)
                   │
          terminal classified
        ┌──────────┴───────────┐
   hard-fail / timeout      success
        │                      │
   writer provably dead?       │
        ├── no ──> QUARANTINE (indeterminate; reconcile on restart — §6.1/§6.3)
        │ yes                  │
        │              [ post-check ]── absent ──> COMMIT (fail-open)   [§4.1]
        │                 │exit≠0   │exit0/timeout→rollback
        ▼                 ▼         ▼
     ROLLBACK ◄───────────┘       COMMIT ── gated on execution-row CAS ──┐   [§6.3]
   (restore include-set to                                              │
    baseline; NO reset --hard;                                     won CAS? ── no ──> reconcile
    overlay journal set §2.3)                                           │ yes          (someone else
        │                                                               │               wrote terminal)
        └────────────► persist failed-attempt journal              COMMIT done
```

---

## 6. Atomicity & quiescence — the load-bearing hard part (T3 · H3/H4/H5/H9)

A workspace transaction is **not** an atomic DB transaction. The honest framing: the filesystem and the `schedule_executions` row are two stores with crash windows between them. The design's job is to make every window **recoverable**, not to pretend it cannot happen.

### 6.1 Quiescence — rollback only when the writer is **provably dead** (H5)

Rollback restores the tree to `baseline`. If the writer is *still running* when rollback fires, it races the restore and corrupts both. Therefore:

- **Rollback requires proof of death**, not a best-effort terminate. A backend `POST /stop` that returns 404 ("may have already finished") is **not** proof — it yields an **indeterminate** terminal.
- An indeterminate terminal → **QUARANTINE** (mark the row, leave the tree as-is, reconcile on restart — §6.3), **never a blind rollback.** This is precisely why the 3 transport failures in §0.2 cannot be auto-rolled-back: the backend has no death proof.
- Container-local coordination (§3) makes "provably dead" cheap: the coordinator owns the subprocess handle; `waitpid`/exit-status is local truth, no network inference. This is a concrete argument *for* §3 over backend orchestration.

### 6.2 Concurrent non-task writers (H1 — the quiet killer)

The workspace is not single-writer even when `max_parallel_tasks == 1`. Other writers touch `/home/developer` outside the turn:

| Writer | Evidence | Mitigation |
|---|---|---|
| **15-min git auto-sync** — `git add -A` + commit + push of the whole tree on a timer | `git.py:152` (`_run_auto_sync_once`) driven by `auto_sync.py:run_auto_sync_loop` (900 s, `:44`), **independent of any execution** | **Must be quiesced for the turn.** Auto-sync committing mid-transaction would either capture the turn's uncommitted writes or race the tx ref. Recommend: a coordinator-held lock that pauses auto-sync while a transaction is open (auto-sync already gates on `should_run_auto_sync()`). |
| Interactive terminal / SSH session | `agent_ssh.py`, container exec | Out of scope to quiesce; **path-exclude** or accept that concurrent human edits void the clean-baseline (§2.1 rejects a dirty tree, which catches the common case). |
| File API writes, `mkdir` | `agent_files.py` | Same as terminal. |
| Credential injection / hot-reload (`.env`, `.mcp.json`) | `credentials.py` | Already in the persistent-state allowlist; **path-exclude** from the tx include-set. |
| Shared-folder mounts | `crud.py:663` | Device-boundary exclude (§2.4 / H8). |

**The transaction is only as atomic as the writer set it controls.** v1 must either quiesce these for the turn or scope the include-set to exclude their paths. Auto-sync is the one that *must* be quiesced (it commits); the rest can be path-scoped.

### 6.3 FS↔DB atomicity — state machine + restart reconciliation (H3/H4)

Define a **durable transaction state** alongside the execution row (recommend a `tx_state` column or a `.trinity/journal/tx-<execution_id>.json` the coordinator writes), with these states and crash-window recoveries:

| tx_state | Meaning | Crash here → restart reconciliation |
|---|---|---|
| `open` | baseline recorded, turn running | Writer is dead (process gone) → **QUARANTINE**; operator decides. Never auto-rollback (can't prove what was written). |
| `committing` | post-check passed, commit started | Re-check: did the commit land (working branch == new tree)? Yes → finish DB write. No → re-attempt commit idempotently. |
| `committed_fs` | git commit landed, DB terminal **not yet** written | **The dangerous window.** The CAS write (below) is replayed on restart; the commit is durable in git, so reconciliation just (re)applies the terminal status. |
| `rolling_back` | rollback started | Re-run the idempotent restore (restore-to-baseline is naturally idempotent); then write the FAILED terminal. |
| `rolled_back` / `done` | terminal | No action. |

**CAS-gated commit (H4 — fixes a real, present bug).** `update_execution_status` already returns the CAS winner — `result.rowcount > 0` at `db/schedules.py:1377` (the SUCCESS branch updates `WHERE id=? AND status != 'cancelled'`; the non-success branch `WHERE id=? AND status NOT IN (terminal)`). **The caller discards it** at `task_execution_service.py:896`–908: it proceeds to complete the activity and reset the breaker unconditionally, whether or not it won the terminal transition.

> **Writer asymmetry to converge (ties into H13).** The two terminal writers don't even guard the *same way* today: the SUCCESS path (`:896`) ignores the CAS return outright, while the FAILED path (`:1088–1093`) instead does a `get_execution()` read-then-`status != 'cancelled'` check — a **check-then-act TOCTOU**, not the CAS. The H4 fix must route **both** writers through the CAS return value (lost CAS ⇒ reconcile, never act), so there is exactly one consistent terminal-write gate rather than two divergent ones.

> **The commit must consume the CAS result.** Order: **win the execution-row CAS first, then make the git commit the working branch's new HEAD.** If the CAS is lost (another writer — a cancellation, a lease re-delivery, the watchdog — already wrote a terminal for this `execution_id`), the coordinator must **NOT commit** and must reconcile instead. Committing after losing the CAS is how you bless a rolled-back/cancelled turn's writes as the new baseline. This is the single most important correctness coupling in the design, and it is a one-line bug today.

### 6.4 Reader-race retry (#678) and lease re-delivery — boundary is per-**attempt** (H9)

Two mechanisms reuse the **same `execution_id`** and re-run work:

- **#678 reader-race retry** (`task_execution_service.py:758`; second dispatch `:831`, verified): on a 502 with the reader-race signature, the **same `execution_id`** is re-dispatched **inline, with no workspace rollback** — the tree is left exactly as attempt 1 left it.
- **Lease-expiry re-delivery** (target model, `TARGET_ARCHITECTURE.md:201`): a dead turn's row flips back to `queued` and is re-pulled with the **same `execution_id` + idempotency key**.

Both break a per-`execution_id` transaction boundary. If the boundary keys on `execution_id`:

- attempt 2 cannot get a fresh baseline (the tx ref is already owned), and
- **re-snapshotting before attempt 2 would bless attempt-1's partial damage as the new baseline** — the exact anti-goal.

**Decision: the transaction boundary is per-ATTEMPT, not per-`execution_id`.** Concretely:

- Each attempt records its **own** baseline ref `refs/trinity/tx/<execution_id>/<attempt_number>`.
- Before attempt 2 (reader-race **or** re-delivery), the coordinator **rolls attempt 1 back to attempt 1's baseline**, then attempt 2 snapshots fresh from that restored (clean) baseline.
- This makes lease re-delivery **workspace-safe by construction** (the issue's synergy question): a re-pulled turn always starts from a clean tree, never resumes on leftovers (`TARGET_ARCHITECTURE.md:201` says re-delivery "is the same unit of work, never a half-finished turn resumed" — the transaction is what *makes* that true at the filesystem level).
- **Commit-point ordering w.r.t. the result POST:** commit the git ref **before** the result POST wins the CAS would risk a committed tree with a lost CAS; therefore **win the CAS, then commit** (§6.3), and the result POST carries the terminal. Under pull, the result POST *is* the CAS write — so "CAS then commit" means "the coordinator commits only after its own result POST is accepted."

---

## 7. Opt-in granularity (issue: "Granularity & opt-in")

- **Per-execution opt-in, with an agent-level default** — NOT a bare per-agent flag. Manual exploratory turns often want partial work kept (a research agent accumulating notes); scheduled jobs want rollback. The trigger envelope carries `transactional: true|false`; the agent-level default (`agent_ownership`, default **off**) supplies it when the trigger omits it.
- **Internal executions are excluded**: `triggered_by="validation"` (§4.3 anti-recursion), maintenance/health turns, and the pre-check hook itself never transact.
- **Default off** matches the issue ("many agents want partial persistence").

---

## 8. What already exists — reuse vs rebuild (verified against the branch)

| Primitive | Location (verified) | Verdict |
|---|---|---|
| `build_snapshot` / `restore_from_tar` (allowlist-MATCH tar) | `docker/base-image/agent_server/routers/snapshot.py:67,77` (the issue body's "in `git.py`" is **wrong** — `git.py` only imports/composes them) | **Future non-git/tar track only**, and only after fixing the restore-delete gap (below). NOT the v1 git mechanism. |
| ↳ restore semantics (the gap) | `restore_from_tar` writes/overwrites tar members; it **does not delete files created after the snapshot**, **does not recreate empty dirs** (`_collect_files` is `is_file()`-only), and **skips symlinks** (`extractfile` returns `None`). | A tar-based rollback would leave turn-created files behind — **incorrect as a rollback** until an explicit delete/replace phase is added (H2). |
| `_read_persistent_state()` + default allowlist `["workspace/**", …]` | `files.py:238` (fn), `:229` (default literal) | Read for reference; **do NOT** reuse as the rollback-survives set — it includes `workspace/**` (§2.3). |
| `reset_to_main_preserve_state_impl()` (snapshot→reset→overlay→force-push) | `git.py:1140` | **Pattern reference only.** v1 uses a tx-owned ref, not reset-to-main, and does not force-push. |
| `pre_check_service.py` (docker-exec hook; shebang; stdout/exit contract; 32 KB cap) | `src/backend/services/pre_check_service.py:44` | **Mirror** for the post-check validator (§4). |
| Terminal classification + CAS winner | `task_execution_service.py:896`; `db/schedules.py:1377` | Commit **must** consume the CAS result (today ignored — §6.3). |
| `ValidationService` (dispatches a separate validation turn) | `validation_service.py:127`,`:186` | Keep orthogonal; must not recurse into a transaction (§4.3). |
| auto-sync timer (whole-tree `git add -A`/commit/push, 900 s) | `git.py:152` + `auto_sync.py:44` | **Quiesce during a transaction** (§6.2 / H1). |

---

## 9. Hazards & open design questions (mandatory — OV3)

| # | Hazard | Where it bites | v1 disposition |
|---|---|---|---|
| **H1** | Concurrent non-task writers (auto-sync `git add -A` mid-turn, terminal, file API, cred injection, shared-folder) | §6.2 | Quiesce auto-sync; path-exclude the rest; clean-baseline rejects the common human-edit case. |
| **H2** | `restore_from_tar` has no delete phase (leaves turn-created files; no empty-dir/symlink restore) | §8 | Blocks the tar/non-git track until fixed; not in v1. |
| **H3** | FS↔DB atomicity: crash windows between commit and terminal write | §6.3 | Durable `tx_state` + restart reconciliation table. |
| **H4** | Commit ignores the execution-row CAS winner (present bug) | §6.3 | Gate commit on CAS; lose CAS ⇒ reconcile, never commit. |
| **H5** | "Provably dead" before rollback; best-effort terminate is indeterminate | §6.1 | Container-local `waitpid`; indeterminate ⇒ quarantine, not rollback. |
| **H6** | Clean-baseline precondition | §2.1 | Reject-and-warn on dirty (v1); snapshot-dirty-separately deferred. |
| **H7** | `reset --hard`/`clean -fd` blast radius (untracked wanted files, branch/refs/stash/submodules, ignored creds survive) | §2.2 | **Rejected** as the primitive; scoped `git restore`/`clean -- <include>` only. |
| **H8** | Mounted cross-agent volumes under `/home/developer` (`shared-in/out`) | §2.4 | Device-boundary path-exclude. |
| **H9** | Reader-race retry (#678) & lease re-delivery reuse the same `execution_id` | §6.4 | Boundary is **per-attempt**; rollback attempt 1 before attempt 2. |
| **H10** | Mutable/untrusted validator (turn rewrites its own post-check) | §4.2 | Capture+digest validator pre-turn; no-write contract. |
| **H11** | Agent may have already committed / switched branch before validation | §3, §6.4 | Highest-coupling open question: terminal classification moves agent-side (pull-aligned) vs stays backend-side; v1 recommends agent-side. |
| **H12** | `ValidationService` recursion (validation turn opens a transaction) | §4.3 | Internal executions excluded (§7). |
| **H13** | Non-single terminal-writer paths must participate: watchdog/cleanup, cancellation, inline chat, container-recreate kills (#1037/#1089) | §6.3 | All terminal writers must go through the CAS; the coordinator must treat a CAS loss to any of them as "do not commit." |

**Top open questions for the reviewer:** (1) **H11** — does v1 move terminal classification agent-side? (2) Is **reject-and-warn** on a dirty baseline acceptable for v1, or is snapshot-dirty-separately required day one? (3) Do we quiesce auto-sync via a shared lock, or disable it entirely while transactional mode is on?

---

## 10. NOT in scope (deferred, with rationale)

### 10.1 Non-git (tar) workspaces — future / evidence-pending
Reuses `build_snapshot`/`restore_from_tar` **only if** greenlit, and **only after** the restore-delete phase (H2) is added. Named, not detailed.

### 10.2 Concurrent (`max_parallel_tasks > 1`) — future; folds in #27
Git-worktree-per-execution. Blocked on the **runtime-config-sharing** problem: a fresh worktree does not carry the gitignored `.claude/`, `.env`, `.mcp.json`, `content/` the turn needs. **This is where #27 (CLOSED) lives** — #1095 supersedes it as the worktree implementation track, to be opened only when the concurrent quadrant is greenlit.

### 10.3 External side-effect rollback — #1084
A workspace transaction is **not** global atomicity. Email/Slack/git-push/payment already sent are not un-sent by a filesystem rollback. `TARGET_ARCHITECTURE.md:482` makes effect-scoped idempotency (#1084) the gate for defaulting side-effect-bearing agents into re-delivery; **the workspace transaction must state this limit explicitly so it does not masquerade as global atomicity.**

### 10.4 Implementation & tests
A follow-up issue, **gated on this doc's review + a GO** (§0.5). The §11 matrix is the test plan that issue inherits.

### 10.5 `TODOS.md`
Repo tracks work via GitHub Issues; the follow-up is filed as an issue, not a TODOS file.

---

## 11. Crash-injection test matrix (the test plan the implementation issue inherits)

Not cosmetic. Each row is a fault injected at a specific phase; the assertion is on the **recovered** state.

| # | Injected fault | Expected recovered state |
|---|---|---|
| 1 | Crash between snapshot and run | No tx ref leak; next start re-snapshots from clean `HEAD`. |
| 2 | Crash mid-run (writer killed) | tx_state=`open` → **quarantine**; tree untouched; operator decision. |
| 3 | Crash between post-check pass and commit | tx_state=`committing` → commit re-attempted idempotently; exactly one commit. |
| 4 | Crash after git commit, before DB terminal | tx_state=`committed_fs` → terminal re-applied via CAS; commit durable. |
| 5 | Crash mid-rollback | Idempotent restore re-runs; ends `rolled_back`; journal set intact. |
| 6 | **Dirty baseline** at tx start | Tx rejected; turn runs non-transactionally; warning logged. |
| 7 | **Cancellation during commit** (CAS lost to cancel) | Commit **aborted**; no working-branch advance; reconcile to cancelled. |
| 8 | Rollback failure (restore errors) | tx_state stuck `rolling_back` → retried; surfaced to operator queue if it keeps failing. |
| 9 | Backend/container restart mid-transaction | Restart reconciliation (§6.3) drives each tx_state to a defined terminal. |
| 10 | **Auto-sync collision** (timer fires mid-turn) | Auto-sync quiesced; no mid-tx commit; tx ref intact. |
| 11 | Terminal edit mid-turn (human SSH write) | Caught by clean-baseline reject (6) or path-excluded; never silently discarded. |
| 12 | **Reader-race attempt 2** (#678) | Attempt 1 rolled back to its baseline; attempt 2 snapshots fresh; no attempt-1 leftovers. |
| 13 | **Lease re-delivery** of the same `execution_id` | Re-pulled turn starts from clean tree; per-attempt baseline (§6.4). |
| 14 | Branch change mid-turn (agent `git switch`) | Detected (H11); transaction voids to quarantine rather than committing onto the wrong branch. |
| 15 | Symlinks in include-set | Handled (git tracks symlinks natively) — and proves the tar track's symlink gap (H2) is git-track-only. |
| 16 | Mount-boundary traversal (write into `shared-in`) | Excluded paths untouched by commit/rollback. |
| 17 | Snapshot / disk exhaustion | Tx fails closed (no partial commit); turn runs non-transactionally or errors cleanly. |
| 18 | **Happy path: success-commit** | Validated tree == committed tree; working branch advanced once. |
| 19 | **Happy path: hard-fail rollback** | Non-zero/timeout → tree back to baseline; journal set preserved. |
| 20 | **Happy path: soft-fail rollback** | post-check exit≠0 → tree back to baseline; journal set preserved. |
| 21 | Rollback-survives journal preservation | After any rollback, `.trinity/journal/**` + failure-memory survive. |

---

## 12. Answers to the issue's Research Questions (AC self-check)

| Research question | Answer (section) |
|---|---|
| **Mechanism** (snapshot-in-place vs worktree vs OverlayFS vs CoW) | v1 = **transaction-owned git ref** for the serialized+git quadrant (§2). Worktree = future concurrent track (§10.2). OverlayFS/CoW rejected: host-FS-dependent, violates "commodity hardware / proven primitives." |
| **Concurrency** (snapshot-in-place breaks at `max_parallel>1`) | Confirmed. v1 is **serialized-only**; concurrent needs worktrees and is deferred (§10.2). Not one mechanism for both in v1. |
| **Validation gate** (post-check the right validator? absence handling? soft vs hard) | `~/.trinity/post-check`, **fail-open when absent** (§4.1). Hard-fail = typed `error_code`; soft-fail needs the explicit hook (and is **unevidenced** today, §0.3). |
| **persistent-state allowlist carve-out** | **Inverse is NOT correct as-is** — the allowlist includes `workspace/**`. The rollback-survives set is a **new narrow journal set** (§2.3, D1). |
| **Non-git agents** | Git-native in v1; tar track deferred behind the restore-delete fix (§10.1). |
| **Granularity & opt-in** | **Per-execution**, agent-level default, default off; internal executions excluded (§7). |
| **Lease re-delivery synergy** | Confirmed: per-attempt rollback makes a re-pulled turn workspace-safe (§6.4). Commit ordering: **win CAS → commit**, result POST carries the terminal. |
| **Cost / latency** | Git ref + scoped diff is cheap; sizing query in §0.5 bounds it; hard-excludes (§2.4) keep `node_modules`/caches out. |
| **Relationship to #27** | **#1095 supersedes #27** — #27 becomes the future worktree track (§10.2), opened only if the concurrent quadrant is greenlit. |

**Issue acceptance criteria status after this doc:**

- [x] **Research deliverable (the gate)** — this doc: mechanism selected, Research Questions answered, reachable-from-today path (§13), **reachable NO-GO verdict** (§0).
- [x] **Decision on #27 relationship** — supersedes (§10.2).
- [ ] Workspace transaction boundary implemented (serialized) — *follow-up, gated on GO.*
- [ ] Validation gate wired (post-check; soft+hard rollback; validated success commits) — *follow-up.*
- [ ] persistent-state / journal paths survive rollback — *follow-up (design fixed in §2.3).*
- [ ] Opt-in flag (default off) + no-validator behavior — *follow-up (design fixed in §4.1/§7).*
- [x] **External side effects explicitly NOT rolled back** — §10.3 (links #1084).
- [ ] Tests (hard/soft/success/allowlist/concurrent) — *follow-up (matrix in §11).*
- [ ] `architecture.md` / `TARGET_ARCHITECTURE.md` updated — *follow-up, post-GO.*

---

## 13. Reachable-from-today implementation path (if GO)

Each step is independently shippable and reversible; the order front-loads the cheap correctness fix and the evidence.

1. **Fix H4 first, regardless of GO** — make `task_execution_service.py:896` consume the `update_execution_status` CAS winner. This is a one-line latent bug (a lost-CAS writer proceeds as if it won) and is a prerequisite for *any* commit-gating. (Belongs in its own small PR; arguably ship it independent of this feature.)
2. **Land `~/.trinity/post-check`** mirroring `pre_check_service.py` (`TARGET_ARCHITECTURE.md:276` already plans it) — useful standalone for alerting/validation even with no transaction.
3. **Container-local coordinator skeleton** wrapping the (pull or current) per-task body: clean-baseline check → record baseline ref → run → classify → **no-op commit/rollback** (feature-flagged off). Proves the boundary without acting on it.
4. **Wire rollback/commit** behind the per-execution opt-in (default off), serialized+git only, with the §6.3 `tx_state` + restart reconciliation and §6.2 auto-sync quiesce.
5. **Crash-injection suite** (§11) in CI against a sibling stack before defaulting anything on.

---

## 14. Decision log

| # | Decision | Rationale |
|---|---|---|
| **D1** | Snapshot scope = explicit include-set + hard excludes; rollback-survives set = **new narrow journal set**, separate from the persistent-state allowlist | The allowlist includes `workspace/**`; reusing it neuters rollback (§2.3). |
| **D2** | Mechanism mode-selected by git × concurrency — **narrowed by OV1** to the serialized+git quadrant for v1 | Only the defensible quadrant ships first. |
| **D3** | Serialized path is the shippable target; concurrent is not co-equal (narrowed by OV1) | Concurrent needs worktrees + config-sharing; deferred (§10.2). |
| **OV1** | Reframe to a narrow defensible v1: serialized + clean-baseline git + per-execution opt-in + container-local coordinator, gated behind §0 | Outside-voice (Codex) pass; avoids pretending atomicity/global scope. |
| **OV2** | v1 rollback uses a transaction-owned ref/index + clean-baseline precondition — **NOT** `git reset --hard && git clean -fd` | `reset/clean` blast radius (H7): destroys wanted untracked files; misses branch/refs/stash/submodules; ignored creds & nested repos survive. |
| **OV3** | Mandatory hazards (§9) + crash-injection matrix (§11) | The additive crash/atomicity/concurrency gaps are the real risk; the doc must not hide them. |

---

*Authored as the #1095 research/design gate. **Implementation is gated on this doc's own §0 GO/NO-GO verdict and a human review — not on the plan review that produced it.** On today's evidence the verdict is NO-GO; re-run §0.5 on a representative production instance to revisit.*
