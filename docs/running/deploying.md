# Deploying and operating

## Deployment

A deployment is a set of worker processes, each with a **unique name** —
stable across restarts if you want automatic crash recovery — pointed at the
same database.

### Kubernetes: StatefulSet

A StatefulSet is the natural fit, because each pod has a fixed, unique name
that survives restarts, which is what [crash recovery](#crashed-runners)
relies on:

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: everystep-runner
spec:
  replicas: 3
  serviceName: everystep-runner
  template:
    spec:
      terminationGracePeriodSeconds: 90   # > --drain
      containers:
        - name: runner
          image: your-image
          args: ["everystep_worker", "--pool", "8", "--name", "$(POD_NAME)",
                 "--drain", "30"]
          env:
            - name: POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
          ports:
            - containerPort: 9117        # if --metrics-port 9117
```

- Scale by changing `replicas`; each replica is an independent claimer.
- `terminationGracePeriodSeconds` must be **greater** than `--drain` so the
  worker can exit cleanly before the pod is killed.
- A plain Deployment (ephemeral pod names) works for everything except
  crash recovery: a clean shutdown requeues in-flight runs under any name,
  but a crashed pod leaves its runs parked (see [crashed runners](#crashed-runners)).

### systemd

A plain service unit per worker, with distinct names:

```ini
[Service]
ExecStart=/usr/bin/python /app/manage.py everystep_worker --pool 8 --name everystep-runner-0 --drain 30
TimeoutStopSec=90
```

`TimeoutStopSec` plays the same role as the k8s grace period: it must exceed
`--drain`.

## Rollouts (SIGTERM drain)

On `SIGTERM` (or `SIGINT`) a worker does not abort its work. It stops
claiming, lets the in-flight step of each of its runs finish and be recorded,
and **requeues each run at the next step boundary** — `scheduled`, claim
released — so any runner can claim it and resume it from the recorded steps.
It waits up to `--drain` seconds for the in-flight steps, then exits,
requeueing anything still running first. `--drain 0` waits for in-flight work
indefinitely.

A rolling deploy is therefore lossless with **any** runner names: a worker
that shuts down never leaves a run claimed by it, and recorded progress is
never lost — the next claimer resumes by replay.

**Tuning `--drain`:**

- **below** the supervisor's grace period (k8s
  `terminationGracePeriodSeconds`, systemd `TimeoutStopSec`) — or the worker
  gets killed mid-step, which is fine for correctness (the step re-runs) but
  defeats the point of draining;
- **at or above** your longest-running step — so every in-flight step gets to
  finish and be recorded, instead of being re-executed by the next claimer.

## Crashed runners

A worker that dies without draining — `SIGKILL`, OOM, power loss — cannot
requeue its in-flight runs. They stay `running`, claimed by the dead worker's
name, and no other worker claims them (claims only take `scheduled` rows).
They are recovered by:

- **a restart under the same name**: the startup catchup reclaims the parked
  runs and resumes them by replay. This is why the runner name should
  survive restarts (a k8s StatefulSet gives you that for free);
- **a manual requeue**: an operator sets the rows back to `scheduled` (for
  example from the Django shell) and any worker picks the runs up; or
  re-schedule the work with fresh arguments and, if you use them, fresh
  idempotency keys — the old run keeps its row.

## Data retention

everystep never deletes its own rows. Each run keeps its `Workflow` row plus one
`Step` row per executed step, forever. If that growth matters, prune or
archive from the application side — for example a periodic job that drops
terminal runs older than N days. The UI shows the 200 most recent runs
regardless.
