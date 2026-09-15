# Configuration

everystep has no settings module and reads no environment variables. It is
configured in three places:

1. **Your Django settings** — the app and the database.
2. **The worker command line** — everything a runner does.
3. **Optional extras** — observability integrations, enabled by installing a
   package.

## Django

Add `"everystep"` to `INSTALLED_APPS` and run migrations (see the
[quickstart](getting-started.md)).

The library itself works on any database backend your project uses:
`scheduling` and the models are plain Django. **Workers run on PostgreSQL and
MariaDB.** On PostgreSQL, claiming uses `SELECT ... FOR UPDATE SKIP LOCKED`,
so concurrent workers claim disjoint batches without blocking. MariaDB has no
SKIP LOCKED, so the claim falls back to a plain `FOR UPDATE`: claims stay
exclusive but serialize while a batch is being locked.

## Worker flags

`python manage.py everystep_worker [flags]`:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--pool N` | `4` | Thread pool size: the maximum number of workflows this worker runs at once. |
| `--poll S` | `0.2` | Seconds between claim polls. Bounds how early a due workflow can be picked up. |
| `--name NAME` | hostname | Runner identity: recorded on claimed runs, used by the UI and metrics. Unique among concurrently running workers; keep it identical across restarts so a restart reclaims the runs a crash left behind. See [workers](running/workers.md). |
| `--drain S` | `30` | Seconds to wait for in-flight steps after SIGTERM/SIGINT, at step boundaries, before requeueing the runs and exiting. `0` waits indefinitely. See [rollouts](running/deploying.md#rollouts-sigterm-drain). |
| `--metrics-port N` | `0` (off) | Serve this worker's Prometheus metrics on the given TCP port. Requires the `metrics` extra. |
| `--metrics-bind ADDR` | `0.0.0.0` | Interface to bind the metrics endpoint to (in-cluster scraping). |

## Optional extras

All three degrade to no-ops when not installed: every call site checks for
the package before doing anything.

| Extra | Install | Enables |
| --- | --- | --- |
| `sentry` | `pip install "everystep[sentry]"` | Unhandled workflow failures reported to Sentry, with the workflow's name and id attached. See [Sentry](observability/alerting-tracing.md#sentry). |
| `metrics` | `pip install "everystep[metrics]"` | Prometheus metrics, the per-runner metrics endpoint, and the host-application metrics view. See [metrics](observability/metrics.md). |
| `otel` | `pip install "everystep[otel]"` | OpenTelemetry spans for runs and executed steps on the global tracer `everystep`. See [traces](observability/alerting-tracing.md#opentelemetry). |

## URL mounts

Nothing is mounted for you. Add what you need:

```python
# urls.py
from django.urls import include, path
from everystep import views

urlpatterns = [
    path("everystep/", include("everystep.urls")),          # the UI
    path("everystep/metrics", views.metrics_view),     # Prometheus endpoint with DB-derived queue gauges
]
```

The UI is optional; so is the metrics view (without it, in-process everystep
metrics are still served from your existing `/metrics`, and the per-runner
endpoint is available via `--metrics-port`). Both endpoints are
**unauthenticated** — keep them behind network segmentation or your own
authentication.

## Logging

Worker events (requeued workflows, runs that crashed outside the runner) are
logged through the `everystep` logger. Configure it like any other in your
application; there are no everystep-specific logging settings.
