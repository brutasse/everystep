import socket
import threading
import time
import urllib.request

import pytest
from django.test import RequestFactory

import everystep.metrics as metrics
import everystep.runner as runner
from everystep import schedule, step, workflow
from everystep.errors import SimulatedCrash, Terminal
from everystep.models import Workflow
from everystep.runner import execute
from everystep.views import metrics_view
from everystep.worker import Worker, claim_new
from tests import slow_steps
from tests.helpers import crash_on, re_claim, run_to_completion

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _reset_metrics():
    if metrics.enabled:
        from prometheus_client import REGISTRY

        for collector in list(metrics._collectors):
            REGISTRY.unregister(collector)
        metrics._define()
    yield
    if metrics.enabled:
        from prometheus_client import REGISTRY

        for collector in list(metrics._collectors):
            REGISTRY.unregister(collector)
        metrics._define()


def _value(collector, **labels):
    child = collector.labels(**labels) if labels else collector
    return child._value.get()


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@step
def ok_step():
    return "ok"


@step
def bad_step():
    raise ValueError("boom")


@step
def stop_step():
    raise Terminal("known-reason", {"k": "v"})


@workflow
def good(args):
    return ok_step()


@workflow
def bad(args):
    return bad_step()


@workflow
def stopped_wf(args):
    return stop_step()


@step(unsafe_to_repeat=True)
def marked_step():
    return "m"


@workflow
def blocked_flow(args):
    return marked_step()


@workflow
def two_steps(args):
    ok_step()
    ok_step()
    return "done"


@workflow
def sleeper_flow(args):
    return slow_steps.sleeper()


def test_completed_workflow_records_run_and_step():
    run = run_to_completion(good, {})
    assert run.status == Workflow.Status.COMPLETED
    assert (
        _value(metrics._workflow_runs, workflow="tests.test_metrics.good", status="completed") == 1
    )
    assert (
        metrics._workflow_duration.labels(
            workflow="tests.test_metrics.good", status="completed"
        )._sum.get()
        > 0
    )
    assert (
        _value(
            metrics._step_runs,
            workflow="tests.test_metrics.good",
            step="tests.test_metrics.ok_step",
            status="done",
        )
        == 1
    )
    assert (
        metrics._step_duration.labels(
            workflow="tests.test_metrics.good", step="tests.test_metrics.ok_step"
        )._sum.get()
        > 0
    )


def test_failed_workflow_records_workflow_and_step():
    run = run_to_completion(bad, {})
    assert run.status == Workflow.Status.FAILED
    assert (
        _value(metrics._workflow_runs, workflow="tests.test_metrics.bad", status="failed") == 1
    )
    assert (
        _value(
            metrics._step_runs,
            workflow="tests.test_metrics.bad",
            step="tests.test_metrics.bad_step",
            status="failed",
        )
        == 1
    )


def test_stopped_workflow_records_stopped_status():
    run = run_to_completion(stopped_wf, {})
    assert run.status == Workflow.Status.STOPPED
    assert (
        _value(metrics._workflow_runs, workflow="tests.test_metrics.stopped_wf", status="stopped")
        == 1
    )


def test_blocked_workflow_records_blocked_status():
    run = schedule(blocked_flow, {})
    claimed = claim_new(1, "b-w")
    assert [w.id for w in claimed] == [run.id]
    runner.fault = crash_on("1")
    with pytest.raises(SimulatedCrash):
        execute(run.id)
    runner.fault = None

    re_claim(run)
    execute(run.id)
    run.refresh_from_db()
    assert run.status == Workflow.Status.BLOCKED
    assert (
        _value(metrics._workflow_runs, workflow="tests.test_metrics.blocked_flow", status="blocked")
        == 1
    )
    metrics.update_queue_gauges()
    assert _value(metrics._workflows_blocked) == 1


def test_replayed_step_is_not_recounted():
    # Step 2 crashes before its record is written; on replay step 1 is served
    # from the store (not re-executed) and only step 2 runs again.
    run = schedule(two_steps, {})
    claimed = claim_new(1, "crash-w")
    assert [w.id for w in claimed] == [run.id]
    runner.fault = crash_on("2")
    with pytest.raises(SimulatedCrash):
        execute(run.id)
    assert (
        _value(
            metrics._step_runs,
            workflow="tests.test_metrics.two_steps",
            step="tests.test_metrics.ok_step",
            status="done",
        )
        == 1
    )
    assert (
        _value(metrics._workflow_runs, workflow="tests.test_metrics.two_steps", status="completed")
        == 0
    )

    runner.fault = None
    re_claim(run)
    execute(run.id)
    run.refresh_from_db()
    assert run.status == Workflow.Status.COMPLETED
    assert (
        _value(
            metrics._step_runs,
            workflow="tests.test_metrics.two_steps",
            step="tests.test_metrics.ok_step",
            status="done",
        )
        == 2
    )
    assert (
        _value(metrics._workflow_runs, workflow="tests.test_metrics.two_steps", status="completed")
        == 1
    )


def test_worker_records_pool_claims_and_inflight(monkeypatch):
    monkeypatch.setenv("EVERYSTEP_TEST_STEP_SLEEP", "1")
    slow_steps.SLEEPER_STARTED.clear()
    schedule(sleeper_flow, {})
    worker = Worker(pool_size=2, poll=0.05, name="m-w", drain=5)
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if _value(metrics._worker_inflight, runner="m-w") == 1 and slow_steps.SLEEPER_STARTED.is_set():
                break
            time.sleep(0.05)
        assert _value(metrics._worker_inflight, runner="m-w") == 1
        assert slow_steps.SLEEPER_STARTED.is_set()
    finally:
        worker.stop()
    thread.join(timeout=15)
    assert not thread.is_alive()
    assert _value(metrics._worker_pool_size, runner="m-w") == 2
    assert _value(metrics._worker_claims, runner="m-w") == 1
    assert _value(metrics._worker_inflight, runner="m-w") == 0
    assert _value(metrics._worker_started_at, runner="m-w") > 0
    assert (
        _value(
            metrics._workflow_runs, workflow="tests.test_metrics.sleeper_flow", status="completed"
        )
        == 1
    )


def test_queue_gauges():
    for _ in range(3):
        schedule(good, {})
    metrics.update_queue_gauges()
    assert _value(metrics._workflows_pending) == 3
    assert _value(metrics._workflows_running) == 0
    assert 0 <= _value(metrics._workflows_oldest_pending_age) < 5

    claimed = claim_new(1, "q-w")
    execute(claimed[0].id)
    metrics.update_queue_gauges()
    assert _value(metrics._workflows_pending) == 2
    assert _value(metrics._workflows_running) == 0

    for wf in claim_new(2, "q-w"):
        execute(wf.id)
    metrics.update_queue_gauges()
    assert _value(metrics._workflows_pending) == 0
    assert _value(metrics._workflows_running) == 0
    assert _value(metrics._workflows_oldest_pending_age) == 0


def test_view_serves_metrics():
    response = metrics_view(RequestFactory().get("/everystep/metrics"))
    assert response.status_code == 200
    assert "text/plain" in response["Content-Type"]
    body = response.content.decode()
    assert "everystep_workflows_pending" in body
    assert "# TYPE everystep_workflow_runs_total counter" in body


def test_worker_serves_metrics_endpoint():
    port = _free_port()
    worker = Worker(pool_size=1, poll=0.05, name="ep-w", drain=1, metrics_port=port)
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        body = None
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as resp:
                    body = resp.read().decode()
                break
            except OSError:
                time.sleep(0.05)
        assert body is not None
        assert f'everystep_worker_pool_size{{runner="ep-w"}} 1.0' in body
        assert "# TYPE everystep_workflow_runs_total counter" in body
    finally:
        worker.stop()
    thread.join(timeout=10)
    assert not thread.is_alive()
    with pytest.raises(OSError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)


def test_metrics_port_requires_extra(monkeypatch):
    monkeypatch.setattr(metrics, "enabled", False)
    worker = Worker(pool_size=1, name="no-extra", metrics_port=9999)
    with pytest.raises(RuntimeError, match="everystep\\[metrics\\]"):
        worker._start_metrics_server()


def test_metrics_are_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(metrics, "enabled", False)
    run_to_completion(good, {})
    from prometheus_client import generate_latest

    # No samples may be produced, though the registered collectors' header
    # lines still render.
    lines = [l for l in generate_latest().decode().splitlines() if not l.startswith("#")]
    assert not any(l.startswith("everystep_workflow_runs_total") for l in lines)
    assert not any(l.startswith("everystep_step_runs_total") for l in lines)
    assert not any(l.startswith("everystep_step_duration_seconds") for l in lines)
