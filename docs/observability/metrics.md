# Metrics

everystep exposes Prometheus metrics for workflow processing health and runner
health. Install the extra to enable them; without it, all metric recording
is a no-op.

```
pip install "everystep[metrics]"
```

All everystep collectors register in the **default** `prometheus_client`
registry, so a metrics endpoint in the host application already serves
in-process everystep metrics (for example from an embedded worker) with no
configuration.

## Serving the metrics

**Runner endpoint.** A worker can serve the metrics of its own process —
runner health, pool utilization, and per-workflow execution — on an HTTP
endpoint:

```
python manage.py everystep_worker --metrics-port 9117
```

Prometheus then scrapes `http://<runner>:9117/`. The endpoint is
unauthenticated; keep it behind network segmentation. `--metrics-bind`
controls the interface (default `0.0.0.0`, for in-cluster scraping).

**Host application.** To also expose the queue state computed from the
database, mount the provided view:

```python
# urls.py
from everystep import views

urlpatterns = [
    path("everystep/metrics", views.metrics_view),
]
```

It refreshes the queue gauges on every scrape. For a custom scrape handler,
call `everystep.metrics.update_queue_gauges()` before rendering.

## The metrics

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `everystep_workflow_runs_total` | counter | `workflow`, `status` | Runs ended, by final status (`completed`, `failed`, `stopped`, `blocked`). |
| `everystep_workflow_duration_seconds` | histogram | `workflow`, `status` | Wall time from claim to terminal state. |
| `everystep_step_runs_total` | counter | `workflow`, `step`, `status` | Step executions, by outcome (`done`, `failed`). |
| `everystep_step_duration_seconds` | histogram | `workflow`, `step` | Step execution time. |
| `everystep_worker_pool_size` | gauge | `runner` | The worker's thread pool size. |
| `everystep_worker_inflight` | gauge | `runner` | Workflows currently in flight in this worker. |
| `everystep_worker_claims_total` | counter | `runner` | Workflows claimed (including startup catchup). |
| `everystep_worker_requeued_total` | counter | `runner` | In-flight workflows requeued when the drain deadline expired. |
| `everystep_worker_started_at_seconds` | gauge | `runner` | Unix time the worker started (uptime). |
| `everystep_workflows_pending` | gauge | — | Workflows scheduled and waiting for a claim. |
| `everystep_workflows_running` | gauge | — | Workflows currently running. |
| `everystep_workflows_blocked` | gauge | — | Workflows blocked awaiting a human decision on an [unsafe-to-repeat](../guides/side-effects.md#unsafe-to-repeat) step. Alert on this: it is the dead-letter queue depth. |
| `everystep_workflows_oldest_pending_age_seconds` | gauge | — | Age of the oldest scheduled workflow. |

The queue gauges (`everystep_workflows_*`) are computed from the database at
scrape time, not by any single process.

## Label cardinality

Labels are bounded to code-defined names — workflow and step **function
names**, and the runner name — never to per-run identifiers. The metric
series count grows with the number of workflows and steps you define, not
with the number of runs.
