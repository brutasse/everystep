import contextlib
import sys
import types

import pytest

from everystep import runner
from everystep import Terminal, schedule, step, workflow
from everystep.errors import SimulatedCrash
from everystep.models import Workflow
from everystep.runner import execute
from everystep.worker import Worker
from tests.helpers import claim_next, crash_on, run_to_completion

pytestmark = pytest.mark.django_db


class _Scope:
    def __init__(self, store):
        self._store = store

    def set_context(self, name, value):
        self._store[name] = value


class FakeSentry:
    """Stand-in for the sentry_sdk module: records captured exceptions
    along with the context set on the scope."""

    def __init__(self):
        self.events = []
        self._scope_store = {}

    def isolation_scope(self):
        @contextlib.contextmanager
        def _scope():
            yield _Scope(self._scope_store)

        return _scope()

    def capture_exception(self, exc):
        self.events.append((exc, dict(self._scope_store)))


@pytest.fixture
def sentry(monkeypatch):
    fake = FakeSentry()
    module = types.ModuleType("sentry_sdk")
    module.isolation_scope = fake.isolation_scope
    module.capture_exception = fake.capture_exception
    monkeypatch.setitem(sys.modules, "sentry_sdk", module)
    return fake


class Boom(Exception):
    pass


@step
def boom():
    raise Boom("kaboom")


@workflow
def failing(args):
    boom()
    return "unreachable"


def test_unhandled_failure_is_reported(sentry):
    run = schedule(failing, {})
    claim_next()
    execute(run.id)
    run.refresh_from_db()

    assert run.status == Workflow.Status.FAILED
    assert len(sentry.events) == 1
    exc, contexts = sentry.events[0]
    assert isinstance(exc, Boom)
    assert str(exc) == "kaboom"
    assert contexts["everystep"] == {
        "workflow": f"{failing.__module__}.{failing.__qualname__}",
        "workflow_id": str(run.id),
    }


@step
def flaky():
    raise Boom("handled")


@workflow
def rescued(args):
    try:
        flaky()
    except Boom:
        return "rescued"


def test_handled_failure_is_not_reported(sentry):
    run = run_to_completion(rescued, {})
    assert run.status == Workflow.Status.COMPLETED
    assert sentry.events == []


def test_simulated_crash_is_not_reported(sentry):
    run = schedule(failing, {})
    claim_next()
    runner.fault = crash_on("1")
    with pytest.raises(SimulatedCrash):
        execute(run.id)
    assert sentry.events == []


@step
def t_first():
    return "1"


@step
def t_second():
    raise Terminal("stop", {"x": 1})


@workflow
def t_flow(args):
    t_first()
    t_second()
    return "ok"


def test_terminal_is_not_reported(sentry):
    run = schedule(t_flow, {})
    claim_next()
    execute(run.id)
    run.refresh_from_db()
    assert run.status == Workflow.Status.STOPPED
    assert sentry.events == []


@workflow
def blobby(args):
    return object()


@pytest.mark.django_db(transaction=True)
def test_runner_escape_is_reported(sentry):
    # _execute closes the thread's DB connection, so the test must not rely on
    # a wrapping transaction to keep its setup data alive.
    run = schedule(blobby, {})
    claimed = claim_next()
    # _execute only fails a run it claims itself (claimed_by == worker name).
    Worker(pool_size=1, name="test-worker")._execute(claimed)
    run.refresh_from_db()

    assert run.status == Workflow.Status.FAILED
    assert len(sentry.events) == 1
    exc, contexts = sentry.events[0]
    assert type(exc).__name__ == "EverystepError"
    assert contexts["everystep"] == {"workflow_id": str(run.id)}


def test_missing_sentry_sdk_is_a_noop():
    run = run_to_completion(failing, {})
    assert run.status == Workflow.Status.FAILED
