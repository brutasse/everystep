import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from django.db import connection, connections

from everystep import schedule, step, workflow
from everystep.models import Step, Workflow
from everystep.worker import Worker, claim_new, resume_own
from tests import slow_steps

pytestmark = pytest.mark.django_db(transaction=True)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _spawn_runner(name, drain, sleep_seconds):
    env = {
        **os.environ,
        "EVERYSTEP_E2E_DB": connection.settings_dict["NAME"],
        "EVERYSTEP_E2E_NAME": name,
        "EVERYSTEP_E2E_DRAIN": str(drain),
        "EVERYSTEP_TEST_STEP_SLEEP": str(sleep_seconds),
    }
    return subprocess.Popen(
        [sys.executable, "-m", "tests.rollout_worker"],
        env=env,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@step
def noop():
    return None


@workflow
def simple(args):
    noop()
    return "done"


@workflow
def only_sleeper(args):
    return slow_steps.sleeper()


@pytest.fixture(autouse=True)
def _reset_sleeper_started():
    slow_steps.SLEEPER_STARTED.clear()
    yield
    slow_steps.SLEEPER_STARTED.clear()


def test_claim_is_exclusive_across_workers():
    for _ in range(6):
        schedule(simple, {})
    results = []
    barrier = threading.Barrier(3)

    def worker(index):
        barrier.wait()
        results.append([w.id for w in claim_new(3, f"worker-{index}")])
        # Threads exit without releasing their thread-local connection;
        # close it the way Worker._execute does, or it outlives the test
        # and blocks the test database teardown.
        connections.close_all()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    claimed = [wf_id for ids in results for wf_id in ids]
    assert len(claimed) == 6
    assert len(set(claimed)) == 6


def test_other_runner_cannot_claim_or_reclaim():
    wf = schedule(simple, {})
    assert [w.id for w in claim_new(1, "w1")] == [wf.id]
    # w1's running workflow is invisible to w2: neither as new work nor as a
    # restart of w2's own in-flight work.
    assert claim_new(1, "w2") == []
    assert resume_own(1, "w2") == []


def test_runner_reclaims_own_workflows_after_restart():
    wf = schedule(simple, {})
    claim_new(1, "w1")  # now running, claimed_by="w1"
    claimed = resume_own(1, "w1")
    assert [w.id for w in claimed] == [wf.id]
    assert claimed[0].claimed_by == "w1"
    assert claimed[0].status == Workflow.Status.RUNNING


def test_worker_catchup_resumes_own_workflows():
    # A workflow w1 was running when it went away; a fresh process with the
    # same name picks it up at startup and completes it.
    wf = schedule(simple, {})
    claim_new(1, "w1")

    worker = Worker(pool_size=1, poll=0.05, name="w1")
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            wf.refresh_from_db()
            if wf.status == Workflow.Status.COMPLETED:
                break
            time.sleep(0.05)
    finally:
        worker.stop()
    thread.join(timeout=10)
    assert not thread.is_alive()
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.COMPLETED


def test_worker_loop_completes_due_workflows():
    for _ in range(2):
        schedule(simple, {})

    worker = Worker(pool_size=2, poll=0.05, name="test-worker")
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if Workflow.objects.filter(status=Workflow.Status.COMPLETED).count() == 2:
                break
            time.sleep(0.05)
    finally:
        worker.stop()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert Workflow.objects.filter(status=Workflow.Status.COMPLETED).count() == 2


def test_command_is_registered():
    from django.core.management import get_commands

    assert "everystep_worker" in get_commands()


def test_drain_completes_inflight_workflow_within_deadline(monkeypatch):
    # The only step is in flight at stop: it finishes inside the drain window,
    # its result is stored, and the worker exits cleanly.
    monkeypatch.setenv("EVERYSTEP_TEST_STEP_SLEEP", "1")
    wf = schedule(only_sleeper, {})

    worker = Worker(pool_size=1, poll=0.05, name="drain-w", drain=5)
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            wf.refresh_from_db()
            if wf.status == Workflow.Status.RUNNING and slow_steps.SLEEPER_STARTED.is_set():
                break
            time.sleep(0.05)
        pending = schedule(only_sleeper, {})
        worker.stop()
        thread.join(timeout=15)
    finally:
        worker.stop()
        thread.join(timeout=5)

    assert not thread.is_alive()
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.COMPLETED
    assert wf.result == "s"
    assert set(Step.objects.filter(workflow_id=wf.id).values_list("step_id", flat=True)) == {"1"}
    # Nothing is claimed after stop: the second workflow stays scheduled.
    assert pending.status == Workflow.Status.SCHEDULED


def test_drain_requeues_at_step_boundary_and_new_name_completes(monkeypatch):
    # Stop lands mid step 2: step 2 finishes and is stored, step 3 never
    # starts, and the workflow is requeued for any runner to pick up. A
    # runner with a different name resumes it from the recorded steps.
    monkeypatch.setenv("EVERYSTEP_TEST_STEP_SLEEP", "1")
    wf = schedule(slow_steps.slow_flow, {})

    worker = Worker(pool_size=1, poll=0.05, name="drain-w", drain=5)
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            wf.refresh_from_db()
            if wf.status == Workflow.Status.RUNNING and slow_steps.SLEEPER_STARTED.is_set():
                break
            time.sleep(0.05)
        worker.stop()
        thread.join(timeout=15)
    finally:
        worker.stop()
        thread.join(timeout=5)

    assert not thread.is_alive()
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.SCHEDULED
    assert wf.claimed_by is None
    assert set(Step.objects.filter(workflow_id=wf.id).values_list("step_id", flat=True)) == {"1", "2"}

    worker2 = Worker(pool_size=1, poll=0.05, name="drain-w2")
    thread2 = threading.Thread(target=worker2.run, daemon=True)
    thread2.start()
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            wf.refresh_from_db()
            if wf.status == Workflow.Status.COMPLETED:
                break
            time.sleep(0.05)
    finally:
        worker2.stop()
        thread2.join(timeout=10)

    assert not thread2.is_alive()
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.COMPLETED
    assert wf.result == "done"
    assert set(Step.objects.filter(workflow_id=wf.id).values_list("step_id", flat=True)) == {"1", "2", "3"}


def test_drain_returns_leftovers_when_deadline_expires():
    worker = Worker(pool_size=1, poll=0.1, name="drain-timeout", drain=0.3)
    wf = schedule(simple, {})
    gate = threading.Event()
    future = worker._executor.submit(gate.wait, 5)
    worker._active[future] = wf
    try:
        # Make sure the task is running (not still queued) before draining.
        deadline = time.time() + 10
        while time.time() < deadline and not future.running():
            time.sleep(0.01)
        assert future.running()
        leftovers = worker._drain()
    finally:
        gate.set()
    assert leftovers == [future]


def test_abandon_requeues_leftovers_when_deadline_expires():
    # A run still in flight when the drain deadline expires is requeued, so
    # nothing is left claimed by the exiting worker.
    worker = Worker(pool_size=1, poll=0.1, name="abandon-w", drain=0.3)
    wf = schedule(simple, {})
    claimed = claim_new(1, "abandon-w")
    assert [w.id for w in claimed] == [wf.id]
    gate = threading.Event()
    future = worker._executor.submit(gate.wait, 5)
    worker._active[future] = wf
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not future.running():
            time.sleep(0.01)
        assert future.running()
        leftovers = worker._drain()
        assert leftovers == [future]
        worker._abandon(leftovers)
    finally:
        gate.set()
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.SCHEDULED
    assert wf.claimed_by is None


def test_sigterm_requeues_and_new_name_completes():
    # Full rollout battle test: a real process gets a real SIGTERM mid-step,
    # exits at the drain deadline having requeued the workflow, and a new
    # process with a different name picks it up where it left off.
    name = "rollout-w"
    wf = schedule(slow_steps.slow_flow, {})
    procs = []
    try:
        proc = _spawn_runner(name, drain=2, sleep_seconds=30)
        procs.append(proc)
        deadline = time.time() + 30
        while time.time() < deadline:
            if Step.objects.filter(workflow_id=wf.id, step_id="1").exists():
                break
            time.sleep(0.1)
        assert Step.objects.filter(workflow_id=wf.id, step_id="1").exists()
        time.sleep(0.5)  # let the sleeper get into its long sleep

        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
        elapsed = time.monotonic() - t0
        assert proc.returncode == 0, f"runner exited with {proc.returncode}: {err.decode()}"
        assert elapsed < 8, f"runner took {elapsed:.1f}s to exit after SIGTERM (drain is 2s)"

        wf.refresh_from_db()
        assert wf.status == Workflow.Status.SCHEDULED
        assert wf.claimed_by is None
        # The in-flight step had not finished before the exit: only step 1 stored.
        assert set(Step.objects.filter(workflow_id=wf.id).values_list("step_id", flat=True)) == {"1"}

        proc = _spawn_runner(f"{name}2", drain=2, sleep_seconds=0)
        procs.append(proc)
        deadline = time.time() + 30
        while time.time() < deadline:
            wf.refresh_from_db()
            if wf.status == Workflow.Status.COMPLETED:
                break
            time.sleep(0.1)
        assert wf.status == Workflow.Status.COMPLETED
        assert wf.result == "done"
        # Step 1 replayed from the store; steps 2 and 3 executed.
        assert set(Step.objects.filter(workflow_id=wf.id).values_list("step_id", flat=True)) == {"1", "2", "3"}
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)


def test_sigkill_parks_and_same_name_resumes():
    # A process killed mid-step cannot requeue its runs: they stay parked,
    # and a restart under the same name resumes them by replay.
    name = "rollout-w"
    wf = schedule(slow_steps.slow_flow, {})
    procs = []
    try:
        proc = _spawn_runner(name, drain=2, sleep_seconds=30)
        procs.append(proc)
        deadline = time.time() + 30
        while time.time() < deadline:
            if Step.objects.filter(workflow_id=wf.id, step_id="1").exists():
                break
            time.sleep(0.1)
        assert Step.objects.filter(workflow_id=wf.id, step_id="1").exists()
        time.sleep(0.5)  # let the sleeper get into its long sleep

        proc.kill()
        proc.wait(timeout=10)

        wf.refresh_from_db()
        assert wf.status == Workflow.Status.RUNNING
        assert wf.claimed_by == name
        assert set(Step.objects.filter(workflow_id=wf.id).values_list("step_id", flat=True)) == {"1"}

        proc = _spawn_runner(name, drain=2, sleep_seconds=0)
        procs.append(proc)
        deadline = time.time() + 30
        while time.time() < deadline:
            wf.refresh_from_db()
            if wf.status == Workflow.Status.COMPLETED:
                break
            time.sleep(0.1)
        assert wf.status == Workflow.Status.COMPLETED
        assert wf.result == "done"
        # Step 1 replayed from the store; steps 2 and 3 executed.
        assert set(Step.objects.filter(workflow_id=wf.id).values_list("step_id", flat=True)) == {"1", "2", "3"}
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
