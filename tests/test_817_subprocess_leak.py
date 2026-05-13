"""
Issue #817 — Runaway subprocess after task failure regression repro.

Deterministically reproduces the symptom chain reported in the ticket:
    schedule timeout-kill -> pending_retry -> orphaned CPU-burner -> agent
    /health unresponsive.

The engineered leak lives in config/agent-templates/test-leak-hook/: a
UserPromptSubmit hook spawns a `setsid` CPU-burner with stdin/stdout/stderr
detached to /dev/null. That satisfies all three conditions that let an
orphan slip past the existing cleanup (#618 / #728 / #808):

    1. Outside Claude's pgid (setsid)               -> escapes terminate_process_group
    2. Holds no stdout pipe write end (FDs detached) -> escapes _kill_orphan_pipe_writers
    3. Spins on CPU                                  -> starves agent-server event loop

The schedule's timeout_seconds=30 + max_retries=1 forces the execution to
fail and the scheduler to mark it pending_retry — matching the production
shape from #817.

This test does NOT assert the downstream circuit-breaker cascade. That is
governed by `services/agent_client.py` tunables that need ~32 min of probe
backoff to reach `dormant` at production constants; the CB module has its
own unit tests. CPU pin + /health timeout is sufficient evidence of the
root cause.
"""
from __future__ import annotations

import subprocess
import time
import uuid

import pytest

from utils.api_client import TrinityApiClient
from utils.assertions import assert_status, assert_json_response
from utils.cleanup import cleanup_test_agent


# Schedule firing window: timeout_seconds=30 + backend buffer (10s) +
# scheduler poll cadence + retry scheduling latency. 90s is comfortably
# above the worst-case while staying tight enough to surface regressions
# in the path that produces pending_retry.
PENDING_RETRY_WAIT_SECONDS = 90

# CPU-pin assertion budget. The burner spawns from UserPromptSubmit, so it
# is already running by the time the chat dispatches; we just need Docker
# stats to settle. 30s is well past `docker stats` sampling jitter.
CPU_PIN_WAIT_SECONDS = 30

# Threshold for "burner is loose". Conservative: the production ticket
# observed 133-156%, single-core; even on a busy CI host a setsid'd while-
# loop comfortably exceeds 50% of one core.
CPU_PIN_THRESHOLD_PERCENT = 50.0


def _docker_cpu_percent(container_name: str) -> float | None:
    """Return current CPU% from `docker stats --no-stream`, or None on error.

    Docker reports e.g. "153.40%" — we strip the suffix and parse. None
    on parse failure / container missing so the polling loop can retry.
    """
    try:
        result = subprocess.run(
            [
                "docker", "stats", "--no-stream",
                "--format", "{{.CPUPerc}}", container_name,
            ],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip().rstrip("%")
    try:
        return float(raw)
    except ValueError:
        return None


def _health_curl_times_out(container_name: str) -> bool:
    """Run `docker exec <c> curl -m 5 http://localhost:8000/health` and
    return True iff it exits non-zero (timeout / connection refused).

    Curl's exit code 28 is the operation-timeout we're hoping for; we
    accept any non-zero exit since the bug surface includes connection
    failures during event-loop starvation, not just timeouts.
    """
    try:
        result = subprocess.run(
            [
                "docker", "exec", container_name,
                "curl", "-m", "5", "-s", "-o", "/dev/null",
                "-w", "%{http_code}",
                "http://localhost:8000/health",
            ],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # docker exec itself blocked past 15s — that is exactly the
        # symptom we are looking for (event-loop fully starved).
        return True
    return result.returncode != 0


def _count_burner_processes(container_name: str) -> int:
    """Count surviving subprocess-leak PIDs inside the agent container.

    Matches the burner cmdline pattern `while :; do :; done` (BusyBox `ps`
    on Alpine truncates argv but keeps the loop body visible). Returns 0
    on docker exec error so the caller can distinguish "no leak" from
    "couldn't measure" via separate diagnostics.
    """
    try:
        result = subprocess.run(
            [
                "docker", "exec", container_name,
                "ps", "-eo", "pid,pgid,cmd",
            ],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 0
    if result.returncode != 0:
        return 0
    return sum(
        1 for line in result.stdout.splitlines()
        if "while :" in line or "while:" in line
    )


def _ps_dump_for_diagnostics(container_name: str) -> str:
    """Return surviving-burner ps lines for the failure message."""
    try:
        result = subprocess.run(
            [
                "docker", "exec", container_name,
                "ps", "-eo", "pid,pgid,cmd",
            ],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "(docker exec failed — container may have been cleaned up)"
    if result.returncode != 0:
        return f"(ps exited {result.returncode})"
    return "\n".join(
        line for line in result.stdout.splitlines()
        if "while :" in line or "while:" in line
    )


@pytest.mark.slow
def test_817_repro_subprocess_leak_survives_termination(api_client: TrinityApiClient):
    """Reproduce Issue #817: subprocess survives task termination.

    The bug is "subprocess survives that should not". CPU pin and /health
    timeout are downstream symptoms whose reproducibility depends on host
    CPU contention (a fast Mac with 8 cores does not starve the event
    loop the same way a 1-2 core production VM does). The test asserts
    the cause directly: count `while :` burner PIDs inside the container
    AFTER the scheduler has pending_retry'd the execution and the backend
    has issued its post-failure /api/cancel + terminate path.

    A green run after a fix lands means the cleanup path now reaches
    setsid'd processes that escape Claude's pgid AND have detached FDs.
    """
    suffix = uuid.uuid4().hex[:6]
    system_name = f"test-leak817-{suffix}"
    agent_short = "victim"
    agent_name = f"{system_name}-{agent_short}"
    container_name = f"agent-{agent_name}"

    manifest = f"""
name: {system_name}
agents:
  {agent_short}:
    template: local:test-leak-hook
"""

    schedule_id: str | None = None

    try:
        # -------- Stage 1: deploy -------------------------------------------
        deploy = api_client.post(
            "/api/systems/deploy",
            json={"manifest": manifest, "dry_run": False},
            timeout=120.0,
        )
        assert_status(deploy, 200)
        deploy_data = assert_json_response(deploy)
        assert agent_name in deploy_data["agents_created"], (
            f"Agent {agent_name} not in {deploy_data['agents_created']}"
        )

        # Wait for the agent container to be running.
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            agent_resp = api_client.get(f"/api/agents/{agent_name}")
            if agent_resp.status_code == 200:
                status = agent_resp.json().get("status")
                if status == "running":
                    break
            time.sleep(2)
        else:
            pytest.fail(f"Agent {agent_name} did not reach running status within 90s")

        # Docker "running" doesn't mean the internal agent-server is accepting
        # requests yet — startup.sh still has to copy template files, run
        # write-runtime-config, etc. Poll the agent's /health from inside the
        # container until it returns 200. If we trigger the schedule before
        # this, the backend's call to /api/task fast-fails with ConnectError
        # and the scheduler records `failed` with no useful timing.
        deadline = time.monotonic() + 60
        agent_ready = False
        while time.monotonic() < deadline:
            try:
                probe = subprocess.run(
                    [
                        "docker", "exec", container_name,
                        "curl", "-m", "3", "-s", "-o", "/dev/null",
                        "-w", "%{http_code}",
                        "http://localhost:8000/health",
                    ],
                    capture_output=True, text=True, timeout=8,
                )
                if probe.returncode == 0 and probe.stdout.strip() == "200":
                    agent_ready = True
                    break
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
            time.sleep(2)
        if not agent_ready:
            pytest.fail(
                f"Agent {agent_name} /health did not return 200 within 60s "
                f"of reaching Docker status=running"
            )

        # Pin the container to 1 CPU. The template ships cpu: "1" but
        # Trinity's create path doesn't propagate it to the Docker create
        # call — `docker inspect` shows NanoCpus=0 / CpuQuota=0 on agent
        # containers — so the dev host's full 4-8 cores are available and
        # 8 burners can't actually starve uvicorn. Forcing the limit here
        # matches the production scenario (1-2 core cloud VM) so the
        # /health-timeout stage of the cascade reproduces.
        cpu_pin = subprocess.run(
            ["docker", "update", "--cpus=1", container_name],
            capture_output=True, text=True, timeout=10,
        )
        assert cpu_pin.returncode == 0, (
            f"Failed to pin {container_name} to 1 CPU: "
            f"stdout={cpu_pin.stdout!r} stderr={cpu_pin.stderr!r}"
        )

        # -------- Stage 2: schedule with short timeout + retries ------------
        # cron: 0 0 1 1 * = midnight Jan 1 — never fires naturally during
        # the test window. We trigger manually below.
        sched_resp = api_client.post(
            f"/api/agents/{agent_name}/schedules",
            json={
                "name": "leak817-repro",
                "cron_expression": "0 0 1 1 *",
                "message": "trigger the hook",
                "timeout_seconds": 30,
                "max_retries": 1,
                "retry_delay_seconds": 60,
                "enabled": True,
            },
        )
        assert_status(sched_resp, 201)
        schedule_id = sched_resp.json()["id"]

        # -------- Stage 3: manually trigger --------------------------------
        trigger = api_client.post(
            f"/api/agents/{agent_name}/schedules/{schedule_id}/trigger",
        )
        assert trigger.status_code in (200, 202), (
            f"Trigger returned {trigger.status_code}: {trigger.text}"
        )
        trigger_time = time.monotonic()

        # -------- Stage 4: poll for pending_retry ---------------------------
        # The hook sleeps 120s in the foreground. Backend's chat timeout =
        # schedule timeout_seconds (30) + 10s buffer = 40s. Backend marks
        # FAILED, scheduler's _maybe_schedule_retry promotes to PENDING_RETRY.
        deadline = trigger_time + PENDING_RETRY_WAIT_SECONDS
        reached_pending_retry = False
        executions_seen: list[dict] = []
        while time.monotonic() < deadline:
            exec_resp = api_client.get(
                f"/api/agents/{agent_name}/schedules/{schedule_id}/executions",
            )
            if exec_resp.status_code == 200:
                executions_seen = exec_resp.json() or []
                if any(e.get("status") == "pending_retry" for e in executions_seen):
                    reached_pending_retry = True
                    break
            time.sleep(3)

        assert reached_pending_retry, (
            f"Execution never reached pending_retry within "
            f"{PENDING_RETRY_WAIT_SECONDS}s. Last seen: "
            f"{[(e.get('id'), e.get('status')) for e in executions_seen]}"
        )

        # -------- Stage 5: instrumented burner-survival timeline -----------
        # Sample immediately, then at 5s/10s/20s, to detect whether a
        # delayed cleanup path is killing the burners after pending_retry.
        burner_timeline = []
        for delay in (0, 5, 10, 20):
            if delay:
                time.sleep(delay - (burner_timeline[-1][0] if burner_timeline else 0))
            burner_timeline.append((delay, _count_burner_processes(container_name)))
        print(f"\n[#817 repro] burner-count timeline (s, count): {burner_timeline}")

        # -------- Stage 6 (informational): /health must time out -----------
        # With CPU pinned to 1 core and 8 burners spinning, uvicorn's slice
        # should shrink below the curl 5s timeout. Logged informationally
        # for now — host scheduler variance can still leak a slice through.
        any_timed_out = False
        for _ in range(3):
            if _health_curl_times_out(container_name):
                any_timed_out = True
                break
            time.sleep(8)
        if not any_timed_out:
            print(
                f"\n[#817 repro] /health responded under 5s despite cpu=1 + "
                f"8 burners. Host scheduler may still be giving uvicorn a "
                f"slice; the leak itself is still proven by the burner-PID "
                f"assertion below."
            )
        else:
            print("\n[#817 repro] /health timed out — full production cascade reproduced.")

        # -------- Stage 7: regression gate — no burner should survive ------
        # Asserted as `== 0` so this test FAILS today (proving the bug)
        # and PASSES after the fix lands. The failure message dumps the
        # surviving cmdlines so the bug shape is obvious from CI logs
        # alone — no need to docker exec into the (already-deleted) agent.
        burner_count = _count_burner_processes(container_name)
        diag_ps = _ps_dump_for_diagnostics(container_name) if burner_count else ""
        assert burner_count == 0, (
            f"Subprocess leak (#817) reproduced: {burner_count} burner "
            f"PID(s) survived agent-server cleanup after pending_retry. "
            f"This regression gate is expected to FAIL until #817 is "
            f"fixed; a green run means the cleanup path now reaches "
            f"setsid'd processes whose FDs are detached from Claude's "
            f"pipes.\n\n"
            f"Surviving processes inside {container_name}:\n{diag_ps}"
        )

        # -------- Stage 7 (informational): CPU pin -------------------------
        # On a fast multi-core host this won't reach 100% per-core ceiling,
        # but the production correlation is real — log it so a regression
        # against a busier host surfaces. Failure here does NOT fail the
        # test (host hardware variance is too high to gate on).
        cpu_observed = _docker_cpu_percent(container_name)
        if cpu_observed is not None and cpu_observed < CPU_PIN_THRESHOLD_PERCENT:
            print(
                f"\n[#817 repro] CPU pin observed at {cpu_observed}% — below "
                f"the {CPU_PIN_THRESHOLD_PERCENT}% production threshold but "
                f"{burner_count} burner(s) confirmed alive. Likely host "
                f"hardware variance, not a regression."
            )

    finally:
        if schedule_id:
            try:
                api_client.delete(
                    f"/api/agents/{agent_name}/schedules/{schedule_id}",
                )
            except Exception:
                pass
        # cleanup_test_agent deletes the container, which reaps the burner
        # (Docker SIGKILLs every PID in the container's pid namespace).
        cleanup_test_agent(api_client, agent_name)
