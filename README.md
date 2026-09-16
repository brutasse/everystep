# Everystep - Durable workflow execution for Django

This library provides a syntax and execution environment for durable
workflows and effecting actions to external systems while maintaining
database consistency. Durable is meant as a guarantee of completion, not a
guarantee of success. Robustness aspects like retries and backoff are user
responsibilities.

**Documentation:** <https://brutasse.github.io/everystep/> — quickstart, the
execution model (replay, at-least-once), guides, deployment and operations,
observability, and the full API reference.

High-level workflow properties:

- Durable storage in SQL, with Django as initial integration target.

- Ability to organize workflows along combinations of sequential or parallel
  steps.

- Immediate start for scheduled workflows: a pool of workers ought to be
  ready to pick up work as early as scheduled. Queue semantics are not a
  goal.

- Transactional scheduling: workflows are meant to be scheduled along with
  the other database changes that led to it being scheduled.

- Results from previous steps available for consumption in the next steps,
  both in the workflow body and from within a step.

## Quickstart

```python
from everystep import parallel, schedule, step, workflow


@step
def create_vm(name, size):
    return cloud_api.create_vm(name=name, size=size)


@step
def attach_ip(vm_id):
    return cloud_api.attach_ip(vm_id)


@step
def setup_security_group(vm_id, group):
    return cloud_api.create_security_group(vm_id, group)


@workflow
def provision_vm(args):
    vm_id = create_vm(args["name"], args["size"])
    ip, _sg = parallel(
        lambda: attach_ip(vm_id),
        lambda: setup_security_group(vm_id, args["name"]),
    )
    return ip
```

1. `pip install everystep`, add `"everystep"` to `INSTALLED_APPS`, then
   `python manage.py migrate`.
2. `schedule(provision_vm, {...})` inside your transaction: on commit the
   workflow becomes claimable, on rollback it is gone.
3. Run a worker (PostgreSQL or MariaDB):
   `python manage.py everystep_worker --pool 8 --poll 0.2 --name everystep-runner-0`.
   The name must be unique among running workers; keep it stable across
   restarts so a restart reclaims the runs a crash left behind.

The details — step identity and naming, reading previous results, `Terminal`
stops, making side effects safe to repeat, or marking them unsafe to repeat,
rollouts and crashed runners, metrics, Sentry, traces, the UI, storage
limits, thread safety — are all in the
[documentation](https://brutasse.github.io/everystep/).

## Development

- `uv sync` — create the venv and install dependencies.
- `uv run pytest` — run the test suite. It starts a throwaway Postgres
  container on a free port and removes it afterwards. Point it at your own
  server with the `EVERYSTEP_TEST_PG_PORT` env var (override the image with
  `EVERYSTEP_TEST_PG_IMAGE`, default `postgres:16`).
- `uv run zensical serve` — preview the documentation.
- Tests can simulate a worker process dying between a step's side effect and
  its record: set `everystep.runner.fault` to a handler `fault(ctx, step_id)`
  that raises `everystep.errors.SimulatedCrash`.
