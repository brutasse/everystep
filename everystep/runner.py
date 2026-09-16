"""Replay-based execution engine.

A claimed workflow's body is re-run from the top on every claim. Step calls
whose outcome is already recorded are served from the Step table; execution
resumes at the first unrecorded step.

`fault` is a test hook: when set, it is called (ctx, step_id) after a step's
side effect has run but before its outcome is recorded. Raising
SimulatedCrash simulates a worker process dying at that point.
"""

import time

from django.db import IntegrityError, transaction
from django.utils import timezone

from everystep import context, metrics, serde, traces
from everystep.context import Context
from everystep.errors import (
    EverystepError,
    DrainOrphan,
    EffectUncertain,
    SimulatedCrash,
    Terminal,
    WorkflowCodeError,
)
from everystep.models import Step, Workflow
from everystep.registry import name_of, registry
from everystep.telemetry import report_workflow_failure

fault = None


def run_step(ctx, func, args, kwargs, everystep_id=None, unsafe_to_repeat=False):
    name = name_of(func)
    step_id = ctx.next_id(everystep_id)
    stored = ctx.outcomes.get(step_id)
    if stored is not None:
        if stored["name"] != name:
            raise WorkflowCodeError(
                f"workflow code changed at step {step_id!r}: previously "
                f"{stored['name']!r}, now {name!r} "
                f"(workflow {ctx.workflow_id}). Workflow bodies must be "
                "deterministic across runs."
            )
        if stored["status"] == Step.Status.FAILED:
            raise serde.decode_exception(stored["error"])
        if stored["status"] == Step.Status.STARTED:
            if unsafe_to_repeat:
                raise EffectUncertain(
                    f"step {step_id!r} ({name}) is marked unsafe to repeat and its "
                    f"outcome is uncertain: a previous attempt started but was not "
                    f"recorded, so the effect may have happened. Verify it in the "
                    f"external system, then resolve run {ctx.workflow_id} with the "
                    "everystep_resolve_step command."
                )
            # No longer marked unsafe to repeat: the current code opts back
            # into at-least-once, so re-execute the step.
        else:
            return stored["result"]

    if ctx.draining is not None and ctx.draining.is_set():
        raise DrainOrphan()

    try:
        serde.dumps(list(args))
        serde.dumps(kwargs)
    except (TypeError, ValueError) as exc:
        raise EverystepError(f"step {name!r} arguments are not serializable: {exc}") from exc

    if unsafe_to_repeat and ctx.persistent:
        # Claim the right to perform the effect before performing it: the
        # unique (workflow, step_id) constraint makes this atomic, and the
        # started row outlives a crash in the effect window.
        try:
            with transaction.atomic():
                Step.objects.create(
                    workflow_id=ctx.workflow_id,
                    step_id=step_id,
                    name=name,
                    args=list(args),
                    kwargs=kwargs,
                    status=Step.Status.STARTED,
                )
        except IntegrityError:
            raise EffectUncertain(
                f"step {step_id!r} ({name}) is marked unsafe to repeat and its "
                f"outcome is uncertain: another attempt is already in flight "
                f"(run {ctx.workflow_id}). Verify it in the external system, then "
                "resolve the run with the everystep_resolve_step command."
            ) from None

    started = time.monotonic()
    with traces.span(name, {"everystep.step.id": step_id}, active=ctx.persistent) as step_span:
        try:
            result = func(*args, **kwargs)
            error = None
        except Exception as exc:
            result = None
            error = exc
        if error is not None:
            traces.mark_error(step_span, error)
        else:
            traces.mark_ok(step_span)
    duration = time.monotonic() - started

    if error is None:
        try:
            serde.dumps(result)
        except (TypeError, ValueError) as exc:
            raise EverystepError(f"step {name!r} result is not serializable: {exc}") from exc

    if ctx.persistent:
        if fault is not None:
            fault(ctx, step_id)
        status = Step.Status.FAILED if error is not None else Step.Status.DONE
        error_payload = serde.encode_exception(error) if error is not None else None
        with transaction.atomic():
            if stored is not None or unsafe_to_repeat:
                # A started row already exists: either this attempt claimed
                # the effect before running it, or a previous attempt left
                # one behind and the step no longer marks itself unsafe to
                # repeat. Close it out in place.
                Step.objects.filter(
                    workflow_id=ctx.workflow_id,
                    step_id=step_id,
                ).update(status=status, result=result, error=error_payload)
            else:
                Step.objects.create(
                    workflow_id=ctx.workflow_id,
                    step_id=step_id,
                    name=name,
                    args=list(args),
                    kwargs=kwargs,
                    status=status,
                    result=result,
                    error=error_payload,
                )
        metrics.record_step(ctx.workflow_name, name, status, duration)
        # Keep the shared outcome store current, so a later step can read
        # this step's outcome via context.current().outcomes. On a replay the
        # outcome is already present from the claim-time snapshot.
        ctx.outcomes[step_id] = {
            "name": name,
            "status": status,
            "result": result,
            "error": error_payload,
        }

    if error is not None:
        raise error
    return result


def execute(workflow_id, draining=None):
    """Run a claimed workflow to a terminal state (or a simulated crash).

    `draining` is an Event the runner sets on stop: the step in flight at
    the signal finishes and is recorded, but no new step starts; the
    DrainOrphan raised at the next step boundary propagates to the caller,
    which requeues the workflow so any runner can claim it.
    """
    started = time.monotonic()
    workflow = Workflow.objects.get(id=workflow_id)
    func = registry.resolve(workflow.name)

    outcomes = {
        row.step_id: {
            "name": row.name,
            "status": row.status,
            "result": row.result,
            "error": row.error,
        }
        for row in Step.objects.filter(workflow_id=workflow.id)
    }

    ctx = Context(
        workflow_id=workflow.id,
        outcomes=outcomes,
        persistent=True,
        draining=draining,
        workflow_name=workflow.name,
    )
    context.set_current(ctx)
    try:
        with traces.span(workflow.name, {"everystep.workflow.id": str(workflow.id)}) as span:
            try:
                result = func(*workflow.args)
                error = None
            except SimulatedCrash:
                span.set_attribute("everystep.workflow.status", "running")
                raise
            except DrainOrphan:
                span.set_attribute("everystep.workflow.status", "running")
                raise
            except Terminal as t:
                updated = Workflow.objects.filter(id=workflow.id, status=Workflow.Status.RUNNING).update(
                    status=Workflow.Status.STOPPED,
                    error=serde.encode_exception(t),
                    completed_at=timezone.now(),
                )
                if updated:
                    metrics.record_workflow_terminal(workflow.name, "stopped", time.monotonic() - started)
                    span.set_attribute("everystep.workflow.status", "stopped")
                    traces.mark_ok(span)
                return
            except EffectUncertain as t:
                updated = Workflow.objects.filter(id=workflow.id, status=Workflow.Status.RUNNING).update(
                    status=Workflow.Status.BLOCKED,
                    error=serde.encode_exception(t),
                    completed_at=timezone.now(),
                )
                if updated:
                    metrics.record_workflow_terminal(workflow.name, "blocked", time.monotonic() - started)
                    span.set_attribute("everystep.workflow.status", "blocked")
                    traces.mark_ok(span)
                return
            except Exception as exc:
                result = None
                error = exc

            now = timezone.now()
            if error is not None:
                report_workflow_failure(error, workflow_id=workflow.id, workflow_name=workflow.name)
                updated = Workflow.objects.filter(id=workflow.id, status=Workflow.Status.RUNNING).update(
                    status=Workflow.Status.FAILED,
                    error=serde.encode_exception(error),
                    completed_at=now,
                )
                if updated:
                    metrics.record_workflow_terminal(workflow.name, "failed", time.monotonic() - started)
                    span.set_attribute("everystep.workflow.status", "failed")
                    traces.mark_error(span, error)
                return
            try:
                serde.dumps(result)
            except (TypeError, ValueError) as exc:
                raise EverystepError(f"workflow result for {workflow.name!r} is not serializable: {exc}") from exc
            updated = Workflow.objects.filter(id=workflow.id, status=Workflow.Status.RUNNING).update(
                status=Workflow.Status.COMPLETED,
                result=result,
                completed_at=now,
            )
            if updated:
                metrics.record_workflow_terminal(workflow.name, "completed", time.monotonic() - started)
                span.set_attribute("everystep.workflow.status", "completed")
                traces.mark_ok(span)
    finally:
        context.clear_current()
