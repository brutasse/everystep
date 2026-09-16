# Data model and storage

Two tables, both created by migrations shipped with the app.

## `everystep_workflow`

One row per scheduled run.

| Field | Type | Notes |
| --- | --- | --- |
| `id` | UUID, primary key | UUIDv7 — time-ordered, so insertion order and id order agree. |
| `name` | varchar(300) | The workflow function's qualified name, `module.qualname`. |
| `args` | jsonb | The positional args exactly as passed to `schedule()`. Replayed verbatim on every claim. |
| `idempotency_key` | varchar(255), null | Set only when `schedule(..., idempotency_key=...)` was used. |
| `status` | varchar(16), indexed | `scheduled`, `running`, `completed`, `failed`, `stopped`, `blocked`. |
| `claimed_by` | varchar(255), null | The name of the worker that claimed the run. |
| `result` | jsonb, null | The workflow's return value, set on `completed`. |
| `error` | jsonb, null | The encoded exception, set on `failed` and `stopped`. |
| `created_at` | timestamptz, indexed | When the row was inserted; the claim order. |
| `completed_at` | timestamptz, null | When the run reached a terminal status. |

Constraint: unique `(name, idempotency_key)` where `idempotency_key` is not
null — what makes `schedule`'s idempotency key safe under concurrency.

## `everystep_step`

One row per **executed** step of a run (steps served from the store on a
replay create no row).

| Field | Type | Notes |
| --- | --- | --- |
| `id` | bigint auto | |
| `workflow` | FK → `everystep_workflow`, CASCADE | Related name `steps`. |
| `step_id` | varchar(300) | The dotpath identity: positional or `everystep_id`-based. |
| `name` | varchar(300) | The function's qualified name, recorded for divergence detection on replay. |
| `args` | jsonb | Positional args as called. |
| `kwargs` | jsonb | Keyword args as called. |
| `status` | varchar(16) | `done` or `failed` — or `started`, written before the effect of an [unsafe-to-repeat](../guides/side-effects.md#unsafe-to-repeat) step runs and updated in place once its outcome is known. |
| `result` | jsonb, null | The return value, when `done`. |
| `error` | jsonb, null | The encoded exception, when `failed` (including `Terminal`). |

Constraint: unique `(workflow, step_id)` — a step's outcome is recorded once.
For an unsafe-to-repeat step the constraint also makes the pre-effect `started`
insert an **atomic claim on the effect**: a racing second claimant cannot
insert, and so cannot run the effect twice.

## JSON storage

`args`, `kwargs`, `result` and `error` are `EverystepJSONField` — Django's
`JSONField` with everystep's extended encodings (`datetime`, `date`, `timedelta`,
`UUID`, `bytes`, `enum` — see [steps](../concepts/steps.md#serialization)).
On PostgreSQL they are stored as `jsonb`.

Exception payloads are plain JSON objects: `{"type": "module.Class",
"message": "...", "args": [...]}`.

## Storage limits

- **Character columns** are bounded as above: workflow name and step id
  300, idempotency key and claimer 255, statuses 16.
- **Step ids** are additionally bounded by the engine at 300 characters for
  the full dotpath; a `everystep_id` that would exceed it raises `EverystepError`
  before the step runs.
- **`args`/`kwargs`/`result`/`error` have no application-level limit.** They
  are bounded only by PostgreSQL (a `jsonb` value can reach the ~1 GB
  TOAST limit). There is no reason to come anywhere close: the UI renders
  these values in full, and they are loaded into memory on every claim.
  Store IDs and references, not payloads. Note that `bytes` are stored
  base64-encoded, i.e. 4/3 of their size.
- **Rows accumulate forever** — everystep deletes nothing. A run costs one
  `Workflow` row plus one `Step` row per executed step. Prune from the
  application side if growth matters (see
  [data retention](../running/deploying.md#data-retention)).

## Migrations

The app ships its migrations; there is nothing to generate. History:

1. initial schema — `Workflow` (UUIDv4 primary key, `claimed_at`) and `Step`;
2. dropped `claimed_at` — the claimer, not a timestamp, is the recovery key;
3. primary key changed to UUIDv7 — time-ordered ids;
4. `stopped` added to the workflow statuses — for `Terminal` stops;
5. `blocked` added to the workflow statuses and `started` to the step
   statuses — choices only, no schema change.
