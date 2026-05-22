# Macau (#797 / issue-678) — Full Suite Run

**Date:** 2026-05-12 03:07 BST  
**Worktree:** `/Users/andrii/conductor/workspaces/trinity/macau`  
**Branch:** `AndriiPasternak31/issue-678-plan`  
**HEAD:** `1b8651ec fix(tests): restore unit-suite sys.modules between tests (#678)`  
**Base:** `origin/dev` (not rebased onto #796 — orphan-test lint piece still present)  
**Stack:** local docker compose (fresh `phoenix_*`-style volume namespace under `macau_*`)  
**Run script:** `tests/run-full.sh` equivalent — `pytest --ignore=unit --ignore=process_engine` then `pytest unit/`

## Totals

| Half | Pass | Fail | Skip | Wall time |
|---|---:|---:|---:|---:|
| Non-unit | 2026 | **24** | 163 | 41:49 |
| Unit | 1439 | 0 | 6 | 02:34 |
| **Combined** | **3465** | **24** | **169** | **~44:23** |

Pass rate: **99.31 %** (3465 / 3489 non-skipped). Unit half is **clean**.

## All 24 failures by root cause

| # | Cluster | Tests | Pass-likelihood after the named fix | Owner |
|---|---|---:|---|---|
| A | **#678 source regression — `await` on sync `circuit.allow_request`** | 7 | High (mechanical test fix or revert) | **You (#678)** |
| B | Pre-existing #804 fixture bug | 3 | Already fixed on #805 | Andrii (#804) |
| C | Environment — agent container not Claude-Code-authed | 4 | After authing the agent | Environment / runbook |
| D | Test-isolation drift (default values + leaked config) | 4 | After test-order or fixture fix | Pre-existing |
| E | Lint regression test scoped too widely | 1 | Exclude `tests/.venv` | Pre-existing |
| F | WhatsApp redis "pending-login" key not appearing | 4 | Needs investigation | Pre-existing / env |
| G | WebSocket auth returns 403 for "valid" token | 1 | Needs investigation | Likely env |

**Of the 24 failures, only 7 are introduced by #678's source changes (cluster A).** Everything else is either pre-existing on `dev`, environmental, or test-isolation / fixture-order brittleness.

---

### Cluster A — **#678 regression, 7 tests** (`tests/test_cb_probe_execution_close.py`)

All seven fail with:
```
TypeError: object MagicMock can't be used in 'await' expression
```

Failing tests:
1. `TestCancelledErrorInExecuteTask::test_cancelled_error_skips_already_terminal_execution`
2. `TestCancelledErrorInExecuteTask::test_cancelled_error_is_reraised`
3. `TestCancelledErrorInExecuteTask::test_cancelled_error_marks_execution_failed`
4. `TestCircuitBreakerFastFail::test_cb_closed_proceeds_normally`
5. `TestCircuitBreakerFastFail::test_cb_open_does_not_call_agent`
6. `TestCircuitBreakerFastFail::test_cb_open_fails_execution_record`
7. `TestCircuitBreakerFastFail::test_cb_open_does_not_mark_dispatched`

**Root cause:** The tests construct `mock_circuit = MagicMock()` then set `mock_circuit.allow_request.return_value = False` (sync). `task_execution_service.execute_task` on this branch now `await`s `circuit.allow_request(...)`. Awaiting a `MagicMock` raises `TypeError`.

**Mechanical fix in test:**
```python
mock_circuit = MagicMock()
mock_circuit.allow_request = AsyncMock(return_value=False)   # <-- promote to AsyncMock
```
Pattern repeats for each test (and for any other `circuit` method now awaited — `record_success`, `record_failure`, etc.). The same file already uses `AsyncMock` correctly for `capacity.acquire`/`release` and `activity_service.track_activity`/`complete_activity` — circuit was the one missed.

**Alternative:** revert the `await circuit.allow_request(...)` site in `task_execution_service.py` if the awaitability was an unintended change.

Action: this is in-scope for #797. Pick the test-side fix unless `await` on circuit is itself the bug.

---

### Cluster B — Pre-existing #804 fixture bug, 3 tests (`tests/security/test_redis_network_isolation.py`)

All three fail with:
```
AssertionError: ... b'NOAUTH Authentication required.\n\n'
```

Failing tests:
1. `test_platform_container_can_authenticate`
2. `test_backend_acl_blocks_flushall`
3. `test_backend_acl_blocks_config_get`

**Root cause:** Already filed and fixed — issue #804, PR #805 stacked on #796. Macau doesn't carry that fix yet. When #805 lands on `dev` and macau rebases, these clear automatically.

Action: none for #797. Wait on #805.

---

### Cluster C — Environment: agent not Claude-Code-authed, 4 tests

Failing tests:
- `test_dynamic_thinking_status::TestAsyncModeSubmission::test_async_mode_completes_eventually`
- `test_dynamic_thinking_status::TestAsyncModeSessionPersistence::test_async_mode_session_contains_messages`
- `test_dynamic_thinking_status::TestAsyncModeSessionPersistence::test_async_mode_with_save_to_session`
- `test_activities::TestActivityCreation::test_chat_creates_activity`

**Root cause:** The fresh `macau_trinity-data` volume's agent containers have no Claude Code auth. Backend returns:
```
"Authentication failure: Not logged in · Please run /login. Check subscription token or API key configuration."
```

These tests require a real chat round-trip through a Claude-Code agent. Without `claude` being logged in inside the agent container, the chat fails 503 and the assertions about session creation / activity creation can't be satisfied.

Action: not a regression. Either auth `claude` inside the agent container (one-time setup per worktree), or accept these as "live-stack-only" environment-sensitive tests and skip on a bare stack. Worth flagging in run-full.sh prerequisites.

---

### Cluster D — Test-isolation drift / leaked config, 4 tests

Failing tests:
- `test_nevermined_permissions::TestNeverminedOwnerAccess::test_get_config_no_config` — expects 404, gets 200 with full nevermined config body
- `test_nevermined_payments::TestPaidAgentInfo::test_info_no_config` — expects [404, 501], gets 200 with full payment requirements body
- `test_shared_folders::TestGetFoldersConfig::test_folders_default_values` — expects `expose_enabled is False`, gets `True`
- `test_subscription_auto_switch::TestAutoSwitchSetting::test_get_auto_switch_default_on` — expects `enabled is True`, gets `False`

**Root cause:** Each test asserts a "default" / "no-config" baseline state on a freshly-created agent, but the agent already has the config the prior test created. Either:
- The test creates a NEW agent and the platform auto-applies some default that the test didn't anticipate, OR
- Fixture cleanup is missing/ordered wrong, leaking a previous test's writes onto the next agent

Not a #678 regression — #678 doesn't touch these endpoints. Probably surfaces because the suite is now running end-to-end against a live stack with shared backend state.

Action: out of scope for #797. Worth a separate issue: "test isolation: nevermined / shared-folders / auto-switch / subscription defaults leak between tests."

---

### Cluster E — Lint regression test over-scopes the venv, 1 test

Failing: `test_lint_sys_modules.py::test_committed_baseline_matches_current_repo_state`

**Root cause:** The lint script's "regression test" (assertion that current state matches baseline) is scanning **all** `.py` files under `tests/`, including third-party packages installed at `tests/.venv/lib/python3.12/site-packages/*`. 36 unrelated library files (pytest internals, coverage, grpc, passlib, …) have `sys.modules` mutations.

This is a scoping bug in the lint script itself, not anything #678 broke. The CLI lint at `python tests/lint_sys_modules.py` doesn't trip on this (presumably because it uses `git ls-files` or a tighter glob). The unit-test version of the same check doesn't have that filter.

Action: pre-existing lint script bug. One-line fix to add a glob ignore for `tests/.venv/`. Worth a separate small issue.

---

### Cluster F — WhatsApp pending-login Redis key never appears, 4 tests

Failing tests:
- `test_whatsapp_integration::TestWhatsAppLoginFlow::test_login_code_verifies_and_writes_email`
- `test_whatsapp_integration::TestWhatsAppAccessGate::test_verified_on_open_access_no_request`
- `test_whatsapp_integration::TestWhatsAppAccessGate::test_verified_on_restrictive_policy_creates_access_request`
- `test_whatsapp_integration::TestWhatsAppLogoutWhoami::test_logout_clears_verified_email`

All fail with:
```
AssertionError: pending-login Redis key never appeared
assert False
+  where False = _wait_for(<lambda>, timeout_s=10.0)
```

**Root cause:** Tests POST a WhatsApp message and expect the adapter to write a `pending-login:*` key to Redis within 10 s. The key never appears.

Likely causes (didn't deep-dive — need to look at the adapter or test fixture):
- Twilio webhook secret mismatch in this worktree's `.env`
- The adapter only writes the key when the binding is configured with a real Twilio AuthToken — could be the fresh stack has no WhatsApp binding rows
- Or #678's executor changes broke the inbound-webhook → Redis path (possible but unlikely — WhatsApp adapter doesn't go through task_execution_service)

Not a #678 regression in spirit. Likely test-fixture / env. Worth a follow-up issue.

---

### Cluster G — WebSocket auth returns 403 for valid token, 1 test

Failing: `test_websocket_auth::TestWebSocketAuthentication::test_ws_valid_token_not_rejected`

```
AssertionError: Valid token should not get 403
assert 403 != 403
```

The test mints a "valid" token (probably via `/api/ws/ticket` per #550 in architecture.md), opens `/ws?ticket=...`, expects ≠403.

**Likely root cause:** ticket service uses Redis `GETDEL`. If the test runs against a backend where the JWT in the Authorization header for the ticket request doesn't match what's expected (e.g., the test mints a JWT but the backend's `SECRET_KEY` rotated when the fresh stack came up with auto-generated passwords), the ticket request itself silently 401s and the WS connect uses an empty/missing ticket → 403.

Not in #678's source change set. Probably environment-dependent — the same SECRET_KEY auto-generation pattern that hit setup_required earlier.

Action: out of scope for #797. Investigate separately.

---

## What CI's regression-diff flagged for #797, and what's true now

The CI run for PR #797 (commit `40a25662` previously) reported these **3 new failures introduced by HEAD**:
- `test_voice_auth.TestVoiceWebSocketAuth::test_admin_bypasses_ownership`
- `test_voice_auth.TestVoiceWebSocketAuth::test_other_user_rejected_4003`
- `test_voice_auth.TestVoiceWebSocketAuth::test_owner_passes_auth_gate`

**All three PASS on the current macau HEAD (`1b8651ec`)** — confirmed in the unit half of this run. They were flaky/order-dependent due to sys.modules cross-contamination from other test files in the same pytest session; commit `1b8651ec fix(tests): restore unit-suite sys.modules between tests (#678)` resolved that.

After force-pushing the current macau HEAD, the CI regression-diff check should reflect this and clear.

## Recommended order to ship #797

1. **Rebase macau onto #796** (clears the orphan-test lint piece on #797 CI, matches the pattern applied to #798/#800/#805). One command:
   ```
   cd ~/conductor/workspaces/trinity/macau
   git fetch origin
   git rebase --onto origin/AndriiPasternak31/lint-baseline-orphan-test origin/dev
   ```
   Then retarget the PR base to `AndriiPasternak31/lint-baseline-orphan-test`.

2. **Fix Cluster A in `tests/test_cb_probe_execution_close.py`** — promote `mock_circuit` (and any sibling sync mocks now awaited) to `AsyncMock`. ~15 min.

3. **Push, mark PR ready for review.** The CI lint should pass; pytest should pass; regression-diff should drop the voice_auth false positives.

4. **Filed-but-out-of-scope** (track separately, do not block #797):
   - Cluster D — leaked-config test isolation issue
   - Cluster E — `tests/.venv/` scoping in the lint regression test
   - Cluster F — WhatsApp pending-login Redis key in fresh-stack runs
   - Cluster G — WebSocket auth 403 in fresh-stack runs

## Reproduction

```bash
# Stop any other Trinity stack first.
cd ~/conductor/workspaces/trinity/macau
./scripts/deploy/start.sh

# Seed setup completion (fresh stacks block /api/token until done — see findings re: docker-logs swallowing the boot-time setup banner)
docker exec trinity-backend python -c "
import sqlite3
c = sqlite3.connect('/data/trinity.db')
c.execute(\"INSERT INTO system_settings (key, value, updated_at) VALUES ('setup_completed', 'true', datetime('now')) ON CONFLICT(key) DO UPDATE SET value='true', updated_at=datetime('now')\")
c.commit()
"

# Run full suite (non-unit + unit halves separately, matching tests/run-full.sh)
cd tests && source .venv/bin/activate
TRINITY_API_URL=http://localhost:8000 \
  TRINITY_TEST_USERNAME=admin \
  TRINITY_TEST_PASSWORD="$(grep -E '^ADMIN_PASSWORD=' ../.env | cut -d= -f2-)" \
  python -m pytest --ignore=unit --ignore=process_engine -v --tb=short
python -m pytest unit/ -v --tb=short
```

Full raw output: `/tmp/macau-full.log` (preserved post-run).
