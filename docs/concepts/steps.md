# Steps

`@step` marks a function as a unit of durable work. Calling it inside a
running workflow:

1. resolves its **step id** (position or `everystep_id` — see
   [identity](identity.md));
2. if the id is already recorded, returns the stored result or re-raises the
   stored exception, without executing the function;
3. otherwise validates that the arguments are serializable, executes the
   function, records the outcome (result **or** exception) in the `Step`
   table, and returns it. A step marked
   [`unsafe_to_repeat`](../guides/side-effects.md#unsafe-to-repeat) is
   additionally recorded as `started` **before** its function runs, so a
   crash in the effect window leaves a row the next replay can refuse to
   repeat.

Each execution is a row:

| Column | Content |
| --- | --- |
| `step_id` | the dotpath identity |
| `name` | the qualified function name, for divergence detection on replay |
| `args`, `kwargs` | what was passed, as recorded |
| `status` | `done` or `failed` — or `started`, for a marked unsafe-to-repeat step between its pre-record and its outcome |
| `result` | the return value, or null |
| `error` | the encoded exception, or null |

## Outside a running workflow

Calling a `@step` function outside a running workflow — in a unit test, in a
management command, from a shell — runs it as a **plain function**: no
context, no recording, no validation. The same goes for `@workflow`
functions and `parallel()` (which still runs its branches concurrently).
This is what makes steps trivially unit-testable.

The `everystep_id` keyword is popped and ignored in that case.

## Serialization

Arguments and results must be JSON-serializable. Beyond plain JSON types,
everystep encodes and decodes these for you:

| Type | Storage |
| --- | --- |
| `datetime` | ISO 8601 string, tagged |
| `date` | ISO 8601 string, tagged |
| `timedelta` | total seconds, tagged |
| `UUID` | string, tagged |
| `bytes` / `bytearray` | base64, tagged |
| `enum.Enum` | the enum class's dotted path + value, tagged |

Values come back as the original types when read from the store, from
`context.current().outcomes`, or as the workflow result.

Two timing details:

- **Arguments are validated before the function runs.** A step called with a
  non-serializable argument fails with `EverystepError` without executing.
- **The result is validated after the function returns.** If the result is
  not serializable, the failure surfaces after the side effect has happened
  and before the record is written — on resume, the step re-runs.

!!! tip
    Store IDs, not model instances. A step should hand its results and arguments
    between steps as data: primary keys, UUIDs, plain values — never Django
    model instances.

## Failed steps

When a step raises, the exception is encoded — the class's dotted path, its
`str()`, and its serializable `args` — and stored on the step row with status
`failed`. The exception also propagates to the workflow body at the call
site.

On replay, a recorded failure is **re-raised** before the step is executed:
the engine reconstructs the original exception type when it is importable,
otherwise it raises `StepFailure` carrying the stored message. This is what
makes durable cleanup possible — see
[errors](errors.md#durable-cleanup).
