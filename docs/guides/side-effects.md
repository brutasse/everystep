# Side effects and re-runs

A step that re-runs after a crash performs its side effect **twice** — the
at-least-once window is described in
[how it works](../concepts/index.md#durability-at-least-once). Either make the
re-run harmless, or mark the step so the engine stops and asks instead of
guessing.

## Safe to repeat

The effect is idempotent: the second execution leaves the system in the same
state as the first.

```python
@step
def tag_vm(vm_id):
    cloud_api.set_tags(vm_id, {"team": "data"})   # re-run writes the same tags
```

Setting a value, deleting something (the `delete_vm` cleanup from the
[errors page](../concepts/errors.md#durable-cleanup)), or converging SQL
(`INSERT ... ON CONFLICT DO UPDATE`, `CREATE INDEX IF NOT EXISTS`) are
idempotent: run the line once or twice, the end state is the same.

### Keyed effects

When the effect is not idempotent on its own, you can make it safe to repeat
by sending it with a **stable key** that the receiving system uses to
recognize and suppress the duplicate — idempotency at the receiver.

```python
@step
def charge_order(order_id, amount):
    return billing_api.charge(
        order_id, amount, idempotency_key=f"charge:{order_id}"
    )
```

If the worker dies after the charge is settled but before the step is
recorded, recovery re-runs the step with the same key and the billing API
returns the original charge instead of charging again. The same pattern
covers HTTP `Idempotency-Key` headers, uniquely named resources, or
`INSERT ... ON CONFLICT DO NOTHING` on a unique column. When the API itself
cannot take a key, wrap the call in a keyed operation at the boundary — a
"reservation" row, or a request id you generate once and persist — rather
than inside the step.

The key must be **identical on every replay of the same step**:

- **From the workflow's arguments** when the key names a logical operation —
  one charge per order, one shipment per order line. Survives re-runs of the
  whole workflow, which is what you want.
- **From the run id** when the key names a single attempt of this run:
  `context.current().workflow_id`. The id is a UUIDv7 fixed when the run was
  scheduled.

These do **not** work:

- timestamps — a re-run computes a new key, and the duplicate passes;
- `uuid4()` — same problem, for the same reason.

## Unsafe to repeat

When you cannot make the effect safe to repeat — a plain
`cloud_api.create_vm(name, size)`, an unkeyed charge, or an unkeyed e-mail —
mark the step:

```python
@step(unsafe_to_repeat=True)
def charge_order(order_id, amount):
    return billing_api.charge(order_id, amount)   # no idempotency key supported
```

The mark changes what the engine does in the
[at-least-once window](../concepts/index.md#durability-at-least-once):

- **Before** the function runs, the step is recorded with status `started`.
  The unique `(workflow, step_id)` constraint makes this insert an atomic
  claim on the effect — no racing claimant can run it a second time.
- **After** the function returns or raises, the row is updated in place to
  `done` or `failed` with the outcome.
- On a later replay, a `started` row with no outcome means the effect may
  have happened. The engine **does not re-execute the step**: the run ends
  in the `blocked` status with an `EffectUncertain` on `run.error`.

A blocked run is a holding state, not a failure: it is never reported to
Sentry, no runner re-claims it, and `everystep_workflows_blocked` measures
how many are waiting on you. After checking the external system, resolve
the run:

```
python manage.py everystep_resolve_step <run_id> <step_id> --result '{"charge_id": "ch_123"}'
python manage.py everystep_resolve_step <run_id> <step_id> --error 'charge declined'
python manage.py everystep_resolve_step <run_id> <step_id> --discard
```

- `--result` — the effect happened; the value becomes the recorded result.
- `--error` — the effect happened and failed; the step is recorded `failed`
  so the body's durable `try/except` cleanup still runs.
- `--discard` — the effect did not happen; the `started` row is deleted and
  the step runs on the next claim.

!!! warning
    A crash can also land **before** the effect: the `started` row is
    written first, then the function runs. If the worker died in that gap,
    the effect never happened and the run is blocked on a false alarm.
    Verify in the external system — that check is the whole point — and use
    `--discard` to let the step run.

Each resolution puts the run back in the queue; any worker resumes it from
the recorded steps.

Two more properties:

- The mark is read from the **current code** on replay. Removing
  `unsafe_to_repeat` from a step whose `started` row survived a crash opts
  that step back into at-least-once: the engine re-executes it.
- A blocked run's `started` step is the only step that can be resolved, and
  a run can only be resolved while it is `blocked`.
