# Quickstart

Requirements:

- Python 3.12 or later
- Django 5.2 or later
- PostgreSQL or MariaDB — required for workers; scheduling itself works on
  any database backend your project uses

## Install

```
pip install everystep
```

Optional extras, each a no-op unless installed:

```
pip install "everystep[sentry]"     # report failed runs to Sentry
pip install "everystep[metrics]"    # Prometheus metrics
pip install "everystep[otel]"       # OpenTelemetry traces
```

## Add the Django app

```python
# settings.py
INSTALLED_APPS = [
    # ...
    "everystep",
]
```

Then migrate:

```
python manage.py migrate
```

This creates the `everystep_workflow` and `everystep_step` tables — see the
[data model](reference/data-model.md) for what they hold.

## Define a workflow

```python
# provision.py
from everystep import parallel, step, workflow


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

Rules of thumb:

- `@workflow` functions are scheduled by name; their return value is the
  persisted workflow result.
- `@step` functions are units of work whose outcomes (result or exception)
  are persisted in SQL.
- Calling a step or workflow outside a running workflow runs it as a plain
  function with no recording, so both are trivially unit-testable.
- Arguments and results must be JSON-serializable (`datetime`, `date`,
  `timedelta`, `UUID`, `bytes` and `enum` are supported). Store IDs, not
  model instances.

## Schedule it

Scheduling is a plain insert into your current transaction:

```python
from django.db import transaction
from everystep import schedule
from provision import provision_vm

with transaction.atomic():
    order.save()
    schedule(provision_vm, {"name": order.vm_name, "size": order.vm_size})
```

On commit the workflow becomes claimable by workers; on rollback it is gone.
The [scheduling guide](guides/scheduling.md) covers idempotency keys and the
details.

## Run a worker

```
python manage.py everystep_worker --pool 8 --poll 0.2 --name everystep-runner-0
```

The worker polls for due workflows, runs them on a thread pool, and replays
them to completion. Give it a name **unique among concurrently running
workers**; keep it identical across restarts (a k8s StatefulSet gives you
both for free) so a restart can reclaim the runs a crash left behind — a
clean shutdown requeues them under any name. See [workers](running/workers.md)
and [deployment](running/deploying.md).

## Watch it run

Mount the bundled UI in your application:

```python
# urls.py
from django.urls import include, path

urlpatterns = [
    path("everystep/", include("everystep.urls")),
]
```

You get a live view of runs, their steps, and the runners — see
[the UI](observability/ui.md).

## What next

- [How it works](concepts/index.md) — replay, at-least-once, and the
  determinism requirement.
- [Configuration](configuration.md) — everything a worker accepts.
- [Naming steps](concepts/identity.md) — do this before your first in-flight
  workflow outlives a deploy.
