# Testing and development

## Running the test suite

```
uv sync
uv run pytest
```

The suite runs on PostgreSQL by default; set `EVERYSTEP_TEST_DB=mariadb` to
run it against MariaDB. It starts a **throwaway container** (`postgres:16`,
or `mariadb:11` for MariaDB) on a free port and removes it afterwards. To
use your own server instead:

| Variable | Default | Meaning |
| --- | --- | --- |
| `EVERYSTEP_TEST_DB` | `postgres` | Backend to run against: `postgres` or `mariadb`. |
| `EVERYSTEP_TEST_PG_PORT` | *(container)* | Point the suite at an existing Postgres server on this port. |
| `EVERYSTEP_TEST_PG_IMAGE` | `postgres:16` | Override the Postgres container image. |
| `EVERYSTEP_TEST_MARIADB_PORT` | *(container)* | Point the suite at an existing MariaDB server on this port. |
| `EVERYSTEP_TEST_MARIADB_IMAGE` | `mariadb:11` | Override the MariaDB container image. |

Most tests run under `@pytest.mark.django_db`, which wraps each test in a
transaction that is rolled back. Tests that need real commits — claiming,
recording, the worker loop — add `transaction=True`.

## Simulating crashes: the fault hook

The interesting behaviours of everystep happen in the window between a step's
side effect and its record. The test suite simulates a worker process dying
there with a fault hook:

```python
from everystep import runner
from everystep.errors import SimulatedCrash

def fault(ctx, step_id):
    if step_id == "2":
        raise SimulatedCrash()

runner.fault = fault
```

`runner.fault` is called with `(ctx, step_id)` **after a step's side effect
has run but before its outcome is recorded**. Raising
`SimulatedCrash` stops the run without writing any further state — the
database is left exactly as it would be if the worker had died at that
instant. Reset `runner.fault = None` afterwards (the suite's `conftest` does
this per test).

`tests/helpers.py` wraps the usual dance:

- `run_to_completion(*args)` — schedule, claim, execute;
- `claim_next()` — claim the next scheduled run;
- `re_claim(run)` — simulate the claiming worker restarting after a crash
  and taking its run back;
- `crash_on(step_id)` — a ready-made fault handler.

The rollout tests go further and spawn **real worker subprocesses**: one gets
a real `SIGTERM` mid-step and must requeue its run for a new process under a
different name; the other is killed mid-step and is resumed by a restart
under the same name.

## Repository layout

| Path | What |
| --- | --- |
| `everystep/` | The library: `api` (the public callables), `runner` (replay engine), `worker` (claim loop), `context`, `serde`, `registry`, `graph` (static step graph for the UI), `models`, `metrics`, `traces`, `telemetry` (Sentry), `views`, `ui`. |
| `demo/` | A runnable Django app with sample workflows and a seeder command — a playground for the UI. |
| `tests/` | The test suite, its settings, and the database container plugin. |
| `docs/` | This documentation, built with Zensical. |

## Building the docs

```
uv run zensical serve    # preview at localhost:8000, rebuilds on change
uv run zensical build    # static site in site/
```
