# Workers

A worker is a long-lived process that claims due workflows and executes them
on a thread pool:

```
python manage.py everystep_worker --pool 8 --poll 0.2 --name everystep-runner-0
```

## The loop

Every `--poll` seconds the worker:

1. reaps finished runs (and logs any run that crashed outside the runner);
2. computes free capacity: `--pool` minus the runs currently in flight;
3. claims up to that many scheduled workflows — `SELECT ... FOR UPDATE SKIP
   LOCKED` in a single transaction, so concurrent workers never claim the
   same row;
4. submits each claimed workflow to its thread pool.

A worker never holds more than `--pool` workflows at once, and claims nothing
new while the pool is full. There is no queue in front of it: a scheduled
workflow is picked up as early as `--poll` allows, by whatever worker has
capacity.

Workers run on **PostgreSQL and MariaDB**. On PostgreSQL the claim uses
`FOR UPDATE SKIP LOCKED`, so concurrent workers never block each other; on
MariaDB it uses a plain `FOR UPDATE`, so claims stay exclusive but serialize
while a batch is being locked.

## The name

Each worker runs under a **name** (`--name`, default: the hostname). The name
is recorded on every run the worker claims, and is what the UI and metrics
use to attribute runs to workers. The contract is:

- **unique among concurrently running workers** — two live workers with the
  same name will both try to resume the same crash-parked runs;
- **identical across restarts** — so that a restart re-claims the runs a
  crash of the previous process left behind (see [startup
  catchup](#startup-catchup)).

A clean shutdown needs no name: in-flight runs are requeued at shutdown, so
a worker coming back under any name picks them up as normal work. A k8s
StatefulSet gives you both properties for free: each pod has a fixed, unique
name.

## Startup catchup

Before entering the poll loop, the worker runs one **catchup**: it claims
back runs parked under its own name — `status = running AND claimed_by =
<name>`, up to pool size — and submits them for replay. After a clean
shutdown the catchup finds nothing, because in-flight runs were requeued at
shutdown; it exists to recover the runs a previous process with the same name
left behind by a crash. In steady state the loop only claims newly scheduled
workflows.

## Shutdown (SIGTERM / SIGINT)

On a stop signal the worker does not abort its work:

1. it **stops claiming** — no new workflow is taken in;
2. it **drains** — the step in flight in each of its workflows runs to the
   end and is recorded, but **no new step starts**: at the next step boundary
   the workflow is **requeued** (`scheduled`, claim released), so any runner
   — under any name — can claim it and resume it from the recorded steps;
3. it **waits** for the in-flight steps up to `--drain` seconds, then exits.
   Anything still running is requeued the same way before the exit.

`--drain 0` waits for in-flight work indefinitely. Nothing is left claimed by
a worker that shuts down: a rolling deploy is lossless even when the
replacement workers run under new names.

## What happens to a run

A run executes on a pool thread. The engine's behaviour — replay, recording,
terminal transitions — is described in
[how it works](../concepts/index.md). Two worker-level behaviours to know:

- If a run dies from something the runner does not handle (a database error
  mid-record, a bug in the engine rather than in your steps), the worker logs
  it, marks the run `failed` with the encoded exception, and reports it to
  Sentry if configured. The worker itself keeps running.
- If the run's row no longer exists (deleted out from under it), the worker
  logs a warning and moves on.

## Embedded workers

`everystep.worker.Worker` is a plain class; you can run a worker inside a host
process instead of a standalone management command:

```python
from everystep.worker import Worker

worker = Worker(pool_size=4, poll=0.2, name="embedded")
thread = threading.Thread(target=worker.run, daemon=True)
thread.start()
# ... later:
worker.stop()
```

Two differences from standalone mode:

- signal handlers are installed only when `run()` executes on the main
  thread; an embedded worker must be stopped with `worker.stop()`;
- when the drain deadline expires with work still in flight, both modes
  requeue the work first; a standalone worker then exits the process, an
  embedded one simply returns and leaves the process lifecycle to its host.
