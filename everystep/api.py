"""Public client API: @workflow, @step, parallel(), schedule()."""

import functools
from concurrent.futures import ThreadPoolExecutor

from django.db import connections

from everystep import context, serde, traces
from everystep.context import Context
from everystep.errors import EverystepError, DrainOrphan, EffectUncertain, Terminal
from everystep.models import Workflow
from everystep.registry import name_of, registry
from everystep.runner import run_step

_WORKFLOW = "workflow"
_STEP = "step"


def workflow(func):
    """Mark a function as a durable workflow.

    The function takes the arguments passed to ``schedule()`` and combines
    ``step`` calls, sequentially or through ``parallel()``. Workflows are
    scheduled by name; the return value is the persisted workflow result.
    Called outside a running workflow, it runs as a plain function.
    """
    if not callable(func):
        raise TypeError("@workflow must decorate a function")

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    wrapper.__everystep__ = _WORKFLOW
    return registry.register_workflow(wrapper)


def step(func=None, *, unsafe_to_repeat=False):
    """Mark a function as a durable step.

    Inside a running workflow, the call is recorded: the outcome (result or
    exception) is persisted in SQL and served from the store on replay, so
    the function body only runs for unrecorded steps. Called outside a
    running workflow, it runs as a plain function with no recording. Pass
    ``everystep_id=...`` at the call site for a stable step identity.

    With ``unsafe_to_repeat=True``, the step's side effect must not happen
    twice. The engine records the step as started before running it, and if
    a replay finds a started step without an outcome — the worker died in
    the effect window — it does not re-execute the step: the run ends in
    the ``blocked`` status until a human resolves it with the
    ``everystep_resolve_step`` management command.
    """
    if func is not None and not callable(func):
        raise TypeError("@step must decorate a function")

    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            everystep_id = kwargs.pop("everystep_id", None)
            ctx = context.current()
            if ctx is None:
                return fn(*args, **kwargs)
            return run_step(ctx, fn, args, kwargs, everystep_id, unsafe_to_repeat)

        wrapper.__everystep__ = _STEP
        return registry.register_step(wrapper)

    if func is not None:
        return decorate(func)
    return decorate


def schedule(workflow_func, *args, idempotency_key=None):
    """Insert a scheduled workflow into the current transaction.

    The workflow becomes claimable by workers once the transaction commits.
    With an idempotency_key, a concurrent or repeated schedule with the same
    key returns the existing workflow instead of creating a new one.
    """
    if getattr(workflow_func, "__everystep__", None) != _WORKFLOW:
        raise TypeError(f"{workflow_func!r} is not a @workflow-decorated function")
    try:
        serde.dumps(list(args))
    except (TypeError, ValueError) as exc:
        raise EverystepError(f"workflow arguments are not serializable: {exc}") from exc

    defaults = {"args": list(args), "status": Workflow.Status.SCHEDULED}
    if idempotency_key is None:
        return Workflow.objects.create(name=name_of(workflow_func), **defaults)
    run, _created = Workflow.objects.get_or_create(
        name=name_of(workflow_func), idempotency_key=idempotency_key, defaults=defaults
    )
    return run


class _Finished:
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class _Failed:
    __slots__ = ("exc",)

    def __init__(self, exc):
        self.exc = exc


def _run_branch(ctx, fork_id, index, branch, otel_ctx=None):
    with traces.attach_context(otel_ctx):
        context.set_current(ctx.branch(fork_id, index))
        try:
            try:
                return _Finished(branch())
            except Exception as exc:
                return _Failed(exc)
        finally:
            context.clear_current()
            # Branches run on pool threads and Django connections are
            # thread-local; release this thread's connection so it is not
            # leaked when the pool thread exits.
            connections.close_all()


def _as_branch(branch):
    if not isinstance(branch, (list, tuple)):
        return branch
    if not branch:
        raise ValueError("parallel() sequence branches must not be empty")
    for call in branch:
        if not callable(call):
            raise TypeError(
                f"parallel() sequence branches take zero-arg callables, got {call!r}"
            )

    def run():
        return tuple(call() for call in branch)

    return run


def parallel(*branches, everystep_id=None):
    """Run zero-arg callables concurrently; return their results in order.

    Each branch is a single step (a @step function or a lambda calling one),
    or a list of zero-arg callables run in order within the branch; such a
    sequence branch returns a tuple of its steps' results.
    If any branch raises, the first error is re-raised (single branch) or an
    ExceptionGroup is raised (several branches).

    Pass everystep_id="..." to give the fork a stable name, so the branch step
    ids ("name.0.1", "name.1.1") do not shift when steps before it change.
    """
    if not branches:
        raise ValueError("parallel() requires at least one branch")
    branches = tuple(_as_branch(branch) for branch in branches)

    ctx = context.current()
    owns_ctx = False
    if ctx is None:
        ctx = Context(workflow_id=None, outcomes={}, persistent=False)
        context.set_current(ctx)
        owns_ctx = True
    try:
        fork_id = ctx.next_id(everystep_id)
        with traces.span("parallel", {"everystep.parallel.id": fork_id}, active=ctx.persistent):
            fork_ctx = traces.capture_context() if ctx.persistent else None
            with ThreadPoolExecutor(
                max_workers=len(branches), thread_name_prefix="everystep-parallel"
            ) as pool:
                futures = [
                    pool.submit(_run_branch, ctx, fork_id, index, branch, fork_ctx)
                    for index, branch in enumerate(branches)
                ]
                outputs = [future.result() for future in futures]
    finally:
        if owns_ctx:
            context.clear_current()

    errors = [output for output in outputs if isinstance(output, _Failed)]
    if not errors:
        return tuple(output.value for output in outputs)
    drains = [e for e in errors if isinstance(e.exc, (DrainOrphan, EffectUncertain, Terminal))]
    if drains:
        raise drains[0].exc
    if len(errors) == 1:
        raise errors[0].exc
    raise ExceptionGroup("everystep parallel branches failed", [e.exc for e in errors])
