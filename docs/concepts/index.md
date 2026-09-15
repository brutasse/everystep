# How it works

This section is the execution model: how a run moves through its states, what
replay means, and what guarantees everystep gives you.

## A run's lifecycle

A workflow run is a `everystep_workflow` row with a status:

```
          claim
scheduled ──────▶ running ──▶ completed
     ▲               ├──▶ failed
     └───────────────└──▶ stopped
          requeue
```

| Status | Meaning |
| --- | --- |
| `scheduled` | Claimable. Set by `schedule()` at commit time, and by a worker requeuing its runs at shutdown. |
| `running` | Claimed; `claimed_by` names the worker. Either actively executing, or parked there by a crashed worker — a worker that shuts down cleanly requeues its runs instead. |
| `completed` | Terminal. `result` holds the workflow's return value; `completed_at` is set. |
| `failed` | Terminal. `error` holds the encoded exception; `completed_at` is set. |
| `stopped` | Terminal, deliberate. A step raised `Terminal`; `error` holds the encoded reason and payload. See [errors](errors.md#stopping-a-workflow-terminal). |

Every executed step is a `everystep_step` row with its own status — `done` or
`failed` — independent of the run's status.

## Claiming

A worker polls its database and claims due work inside a single transaction:

```sql
SELECT id FROM everystep_workflow
WHERE status = 'scheduled'
ORDER BY created_at
LIMIT <free pool capacity>
FOR UPDATE SKIP LOCKED;
-- then, same transaction:
UPDATE everystep_workflow SET status = 'running', claimed_by = '<name>' WHERE id IN (...);
```

`FOR UPDATE SKIP LOCKED` means concurrent workers claim **disjoint** sets:
two workers polling the same table never take the same row. There is no queue
and no backlog — a scheduled workflow is claimed as early as `--poll`
allows, by any available worker. On MariaDB the claim uses a plain
`FOR UPDATE` (no SKIP LOCKED): claims are still disjoint, they just
serialize while a batch is being locked.

## Execution is replay

On every claim — first run, restart, rollout resume — the engine does the
same thing:

1. Load all recorded steps of the run into an outcome store, keyed by step
   id.
2. Re-run the workflow body **from the top**.
3. Each step call looks up its step id in the store:
   - already recorded → the stored result is returned (or the stored
     exception re-raised) **without executing the step**;
   - not recorded → the step actually runs, and its outcome is written to the
     `Step` table.
4. Execution therefore resumes at the **first unrecorded step** and runs to
   the end, or to a failure.

Consequences:

- **Code between step calls re-runs on every resume.** It must be
  deterministic — same inputs, same step calls, in the same order. Workflow
  arguments are fixed at schedule time and replayed verbatim, so deriving
  values from `args` is safe; calling `datetime.now()`, `random()` or
  `uuid4()` between steps is not.
- **The engine checks you.** Each step's qualified function name is recorded
  with its row. On replay, if the function at a given step id is different
  from the recorded one, the run fails loudly with
  `WorkflowCodeError` instead of silently executing the wrong work.
- A step's identity is its **dotpath**: its position in the body, or its
  `everystep_id` name. See [step identity](identity.md).

## Durability: at-least-once

A step is **at-least-once**, not exactly-once:

```
step function runs ──▶ [ crash window ] ──▶ outcome written to SQL
```

If the worker process dies in the crash window — the side effect happened,
the record did not — the next replay finds no record for that step and
**executes it again**. The side effect runs a second time.

This is the whole robustness story of everystep, and it is why:

- durable means *the run reaches a terminal state*, not *the run succeeded*;
- your side effects must be **idempotent or keyed** — see the
  [side effects guide](../guides/side-effects.md);
- steps that fail are recorded as failed and their exception re-raised on
  every replay, so `try/except` cleanup in the body is durable too — see
  [errors](errors.md).
