"""Steps marked unsafe to repeat: the engine claims the effect before
running it, refuses to re-execute an unrecorded attempt, and parks the run
in the blocked (dead-letter) state until a human resolves it."""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from everystep import context, parallel, runner
from everystep import schedule, step, workflow
from everystep.context import Context
from everystep.errors import EffectUncertain, SimulatedCrash, StepFailure
from everystep.models import Step, Workflow
from everystep.registry import name_of
from everystep.runner import execute, run_step
from everystep.worker import claim_new
from tests.helpers import claim_next, crash_on, re_claim

# transaction=True like the other suites that exercise parallel branches:
# branch threads use their own connections, which cannot see a test
# transaction's uncommitted rows.
pytestmark = pytest.mark.django_db(transaction=True)

CALLS = []


@pytest.fixture(autouse=True)
def _calls():
    CALLS.clear()
    yield


@step(unsafe_to_repeat=True)
def charge():
    CALLS.append("charge")
    return "charged"


@step
def confirm():
    CALLS.append("confirm")
    return "confirmed"


@workflow
def paying(args):
    charge()
    return confirm()


@step
def refund():
    CALLS.append("refund")
    return None


@workflow
def guarded_pay(args):
    try:
        charge()
    except StepFailure:
        refund()
        raise
    return "ok"


@step
def plain_effect():
    CALLS.append("plain_effect")
    return "v"


@workflow
def plain_flow(args):
    return plain_effect()


@workflow
def parallel_pay(args):
    a, b = parallel(lambda: charge(), lambda: confirm())
    return a + b


def _crash_in_marked_step(wf, step_id="1"):
    """Claim a run and crash it in the marked step's effect window."""
    runner.fault = crash_on(step_id)
    with pytest.raises(SimulatedCrash):
        execute(wf.id)
    runner.fault = None


def _block(wf):
    """Re-claim the crashed run; it must end blocked without re-running the
    effect."""
    re_claim(wf)
    execute(wf.id)
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.BLOCKED
    assert wf.error["type"] == f"{EffectUncertain.__module__}.{EffectUncertain.__qualname__}"
    return wf


def test_marked_step_completes_and_replays():
    wf = schedule(paying, {})
    claim_next()
    _crash_in_marked_step(wf, step_id="2")

    # Step 1 (marked) recorded done; step 2's effect ran unrecorded.
    assert CALLS == ["charge", "confirm"]
    row = Step.objects.get(workflow_id=wf.id, step_id="1")
    assert row.status == Step.Status.DONE
    assert row.result == "charged"

    re_claim(wf)
    execute(wf.id)
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.COMPLETED
    assert wf.result == "confirmed"
    # The marked step was served from the store; only the unrecorded
    # unmarked step ran again.
    assert CALLS == ["charge", "confirm", "confirm"]


def test_crash_in_effect_window_blocks_the_run():
    wf = schedule(paying, {})
    claim_next()
    _crash_in_marked_step(wf)

    assert CALLS == ["charge"]
    row = Step.objects.get(workflow_id=wf.id, step_id="1")
    assert row.status == Step.Status.STARTED
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.RUNNING

    _block(wf)
    # The effect is not performed a second time.
    assert CALLS == ["charge"]
    # A blocked run sits in the dead-letter state: no runner claims it.
    assert claim_new(1, "other-worker") == []


def test_resolve_with_result():
    wf = schedule(paying, {})
    claim_next()
    _crash_in_marked_step(wf)
    _block(wf)

    call_command("everystep_resolve_step", str(wf.id), "1", "--result", '"charged"')
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.SCHEDULED
    assert wf.claimed_by is None
    assert wf.error is None
    assert wf.completed_at is None
    row = Step.objects.get(workflow_id=wf.id, step_id="1")
    assert row.status == Step.Status.DONE
    assert row.result == "charged"

    claim_next()
    execute(wf.id)
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.COMPLETED
    assert wf.result == "confirmed"
    # The effect was performed exactly once in total.
    assert CALLS == ["charge", "confirm"]


def test_resolve_with_discard_reruns_the_step():
    wf = schedule(paying, {})
    claim_next()
    _crash_in_marked_step(wf)
    _block(wf)

    call_command("everystep_resolve_step", str(wf.id), "1", "--discard")
    assert not Step.objects.filter(workflow_id=wf.id, step_id="1").exists()

    claim_next()
    execute(wf.id)
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.COMPLETED
    assert wf.result == "confirmed"
    assert CALLS == ["charge", "charge", "confirm"]


def test_resolve_with_error_reraises_and_cleans_up():
    wf = schedule(guarded_pay, {})
    claim_next()
    _crash_in_marked_step(wf)
    _block(wf)

    call_command("everystep_resolve_step", str(wf.id), "1", "--error", "charge declined")
    claim_next()
    execute(wf.id)
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.FAILED
    assert wf.error["type"] == f"{StepFailure.__module__}.{StepFailure.__qualname__}"
    assert wf.error["message"] == "charge declined"
    assert CALLS == ["charge", "refund"]


def test_resolve_command_rejects_invalid_requests():
    wf = schedule(paying, {})
    claim_next()
    execute(wf.id)
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.COMPLETED
    with pytest.raises(CommandError):
        call_command("everystep_resolve_step", str(wf.id), "1", "--discard")
    with pytest.raises(CommandError):
        call_command("everystep_resolve_step", "not-a-uuid", "1", "--discard")

    wf2 = schedule(paying, {})
    claim_next()
    _crash_in_marked_step(wf2)
    _block(wf2)
    with pytest.raises(CommandError):
        call_command("everystep_resolve_step", str(wf2.id), "99", "--discard")
    with pytest.raises(CommandError):
        call_command("everystep_resolve_step", str(wf2.id), "1", "--result", "not json")


def test_concurrent_claim_of_the_effect_raises_uncertain():
    # Another claimant holds the effect: its started row exists but is not
    # in this context's outcome snapshot (seeded before the race), so the
    # claim insert must fail instead of running the effect again.
    wf = schedule(paying, {})
    claim_next()
    Step.objects.create(
        workflow_id=wf.id,
        step_id="1",
        name=name_of(charge),
        args=[],
        kwargs={},
        status=Step.Status.STARTED,
    )
    ctx = Context(
        workflow_id=wf.id, outcomes={}, persistent=True, workflow_name=wf.name
    )
    context.set_current(ctx)
    try:
        with pytest.raises(EffectUncertain):
            run_step(ctx, charge.__wrapped__, (), {}, None, True)
    finally:
        context.clear_current()
    assert CALLS == []


def test_started_row_for_unmarked_step_reexecutes():
    # A started row left by a previous attempt of a step that no longer
    # marks itself unsafe to repeat: the current code wins and re-executes.
    wf = schedule(plain_flow, {})
    claim_next()
    Step.objects.create(
        workflow_id=wf.id,
        step_id="1",
        name=name_of(plain_effect),
        args=[],
        kwargs={},
        status=Step.Status.STARTED,
    )
    re_claim(wf)
    execute(wf.id)
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.COMPLETED
    assert wf.result == "v"
    assert CALLS == ["plain_effect"]
    row = Step.objects.get(workflow_id=wf.id, step_id="1")
    assert row.status == Step.Status.DONE
    assert row.result == "v"


def test_uncertain_step_in_parallel_blocks_without_group():
    wf = schedule(parallel_pay, {})
    claim_next()
    _crash_in_marked_step(wf, step_id="1.0.1")
    _block(wf)
    # Each branch's effect ran once; the uncertain step was not re-run.
    assert sorted(CALLS) == ["charge", "confirm"]
