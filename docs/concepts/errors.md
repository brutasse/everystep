# Errors and exceptions

## How failures are recorded

When a step raises, the engine:

1. encodes the exception — the class's dotted path, `str(exc)`, and the
   serializable `args` — and stores it on the step row with status `failed`;
2. re-raises the exception at the step call site, in the workflow body.

If the body does not handle it, the exception propagates out of the run: the
workflow is marked `failed` with the same encoded exception on
`run.error`, and (if configured) it is reported to Sentry.

On replay, a recorded failure is **re-raised before the step executes** —
the original exception type when it is importable, otherwise `StepFailure`
carrying the stored message. The step function itself is not re-run.

## Durable cleanup

Because failed steps re-raise on replay, plain `try/except` in the body is a
durable cleanup mechanism:

```python
@workflow
def provision_vm(args):
    vm_id = create_vm(args["name"], args["size"])
    try:
        ip = attach_ip(vm_id)
    except Exception:
        delete_vm(vm_id)   # a regular step; replay-safe
        raise
    return ip
```

If `attach_ip` fails, `delete_vm` runs, and the run ends `failed`. If the
worker dies between the two — or mid-`delete_vm` — the next replay
re-raises the stored `attach_ip` failure, takes the `except` branch again,
and re-runs `delete_vm`.

!!! warning
    Cleanup steps are at-least-once like all steps: `delete_vm` above can run
    once **per pass**, i.e. more than once across crashes. Make cleanup idempotent
    or keyed like any other side effect — see the
    [side effects guide](../guides/side-effects.md).

## Decoding stored exceptions

`everystep.serde.decode_exception(payload)` turns a stored exception dict back
into an exception instance: the original type when importable, otherwise a
`StepFailure`. This is how a caller reads a failed run or a `stopped` run
back out of `run.error`.

## Stopping a workflow: `Terminal`

A step can stop the workflow deliberately, for a known condition, instead of
letting it fail:

```python
from everystep import Terminal, step


@step
def upload(node_id, data):
    result = storage.upload(node_id, data)
    if result.node_full:
        # Hand off to the caller: it picks another node and re-schedules.
        raise Terminal("node-full", {"node_id": node_id})
    return result
```

Raising `Terminal(reason, payload)`:

- lets the step in flight run to completion and be recorded (its row shows
  status `failed` with the encoded `Terminal`);
- stops the run before the next step starts;
- ends the workflow in status **`stopped`** with the encoded exception on
  `run.error`;
- is **not** a failure: it is never reported to Sentry and ends its trace
  span cleanly.

The caller takes over by decoding `run.error`:

```python
from everystep import serde
from everystep.errors import Terminal

t = serde.decode_exception(run.error)
if isinstance(t, Terminal) and t.reason == "node-full":
    other = pick_another_node()
    schedule(upload_to_node, {"node": other, "data": data})
```

`Terminal` inside a `parallel` branch stops the whole run: it is raised
ahead of any `ExceptionGroup` the fork would build.

## Blocked runs: uncertain effects

A step marked [`unsafe_to_repeat`](../guides/side-effects.md#unsafe-to-repeat)
is recorded as `started` before its side effect runs. If the worker dies in
the effect window — or a second claimant races the first — the next replay
finds a `started` row with no outcome. The effect may or may not have
happened, and only the external system can say. The engine therefore:

- does **not** execute the step again;
- ends the run in the **`blocked`** status with an `EffectUncertain` on
  `run.error`;
- reports nothing to Sentry: a blocked run is a holding state, not a
  failure. It is the engine's dead-letter queue, and
  `everystep_workflows_blocked` measures its depth.

A human resolves the run after checking the external system:

```
python manage.py everystep_resolve_step <run_id> <step_id> --result '<json>'
python manage.py everystep_resolve_step <run_id> <step_id> --error '<message>'
python manage.py everystep_resolve_step <run_id> <step_id> --discard
```

- `--result` — the effect happened; the JSON value becomes the step's
  recorded result and later steps read it as usual.
- `--error` — the effect happened and failed; the step is recorded `failed`
  with that message, so durable `try/except` cleanup in the body still runs
  on the next pass.
- `--discard` — the effect did **not** happen; the `started` row is deleted
  and the step executes on the next claim. Use this when the crash landed
  before the effect: the `started` row is a false alarm, and discarding is
  what lets the step run at all.

Each resolution puts the run back in the queue as `scheduled`, where any
worker resumes it from the recorded steps. A run may only be resolved while
it is `blocked`, and only its `started` step may be resolved.

## everystep's own exception types

| Exception | Meaning |
| --- | --- |
| `EverystepError` | Base class for everystep errors: bad `everystep_id`, non-serializable arguments or results, duplicate names in a scope. |
| `WorkflowCodeError` | The body at a recorded step id no longer calls the recorded function: the code diverged from an in-flight run. The run fails loudly instead of executing the wrong work. |
| `StepFailure` | A replayed recorded failure whose original exception type is unavailable. Carries the stored message. |
| `EffectUncertain` | A step marked unsafe to repeat may have performed its effect; the run ends `blocked` instead of re-executing it. A holding state, never reported to Sentry; resolved with `everystep_resolve_step`. |
| `SimulatedCrash` | Raised only from the test `fault` hook to simulate a process death. Never raised in production code paths. |
| `DrainOrphan` | Internal control flow: raised at a step boundary while the worker drains after a stop signal; the worker requeues the run so any runner can claim it. You neither raise nor catch this. |
