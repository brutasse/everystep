"""Prometheus metrics for workflow processing and runner health.

Requires the optional `metrics` extra (pip install "everystep[metrics]"). Without
prometheus_client, every function in this module is a no-op.

All collectors register in the default prometheus_client registry, so a host
application's existing scrape endpoint serves them with no configuration.
Labels are bounded to code-defined names (workflow and step function names,
runner name), never to per-run identifiers.
"""

import time

try:
    import prometheus_client
except ImportError:
    prometheus_client = None

enabled = prometheus_client is not None

if enabled:
    content_type = prometheus_client.CONTENT_TYPE_LATEST
else:
    content_type = "text/plain; charset=utf-8"

_collectors = []


def _define():
    """(Re)create all everystep collectors.

    Called at import time; tests unregister `metrics._collectors` from the
    registry and call this again for a clean state.
    """
    global _workflow_runs, _workflow_duration, _step_runs, _step_duration
    global _worker_pool_size, _worker_inflight, _worker_claims, _worker_requeues
    global _worker_started_at, _workflows_pending, _workflows_running
    global _workflows_blocked, _workflows_oldest_pending_age, _collectors

    _workflow_runs = prometheus_client.Counter(
        "everystep_workflow_runs",
        "Workflow runs ended, by final status.",
        labelnames=("workflow", "status"),
    )
    _workflow_duration = prometheus_client.Histogram(
        "everystep_workflow_duration_seconds",
        "Wall time from claim to terminal state, by final status.",
        labelnames=("workflow", "status"),
    )
    _step_runs = prometheus_client.Counter(
        "everystep_step_runs",
        "Step executions, by outcome.",
        labelnames=("workflow", "step", "status"),
    )
    _step_duration = prometheus_client.Histogram(
        "everystep_step_duration_seconds",
        "Step execution time.",
        labelnames=("workflow", "step"),
    )
    _worker_pool_size = prometheus_client.Gauge(
        "everystep_worker_pool_size",
        "Worker thread pool size.",
        labelnames=("runner",),
    )
    _worker_inflight = prometheus_client.Gauge(
        "everystep_worker_inflight",
        "Workflows currently in flight in the worker.",
        labelnames=("runner",),
    )
    _worker_claims = prometheus_client.Counter(
        "everystep_worker_claims",
        "Workflows claimed by the worker.",
        labelnames=("runner",),
    )
    _worker_requeues = prometheus_client.Counter(
        "everystep_worker_requeued",
        "In-flight workflows requeued when the drain deadline expired.",
        labelnames=("runner",),
    )
    _worker_started_at = prometheus_client.Gauge(
        "everystep_worker_started_at_seconds",
        "Unix time the worker started.",
        labelnames=("runner",),
    )
    _workflows_pending = prometheus_client.Gauge(
        "everystep_workflows_pending",
        "Workflows scheduled and waiting for a claim.",
    )
    _workflows_running = prometheus_client.Gauge(
        "everystep_workflows_running",
        "Workflows currently running.",
    )
    _workflows_blocked = prometheus_client.Gauge(
        "everystep_workflows_blocked",
        "Workflows blocked awaiting a human decision on an uncertain step effect.",
    )
    _workflows_oldest_pending_age = prometheus_client.Gauge(
        "everystep_workflows_oldest_pending_age_seconds",
        "Age of the oldest scheduled workflow.",
    )
    _collectors = [
        _workflow_runs,
        _workflow_duration,
        _step_runs,
        _step_duration,
        _worker_pool_size,
        _worker_inflight,
        _worker_claims,
        _worker_requeues,
        _worker_started_at,
        _workflows_pending,
        _workflows_running,
        _workflows_blocked,
        _workflows_oldest_pending_age,
    ]


if enabled:
    _define()


def record_step(workflow, step, status, duration):
    if not enabled or workflow is None:
        return
    _step_runs.labels(workflow=workflow, step=step, status=status).inc()
    _step_duration.labels(workflow=workflow, step=step).observe(duration)


def record_workflow_terminal(workflow, status, duration):
    if not enabled or workflow is None:
        return
    _workflow_runs.labels(workflow=workflow, status=status).inc()
    _workflow_duration.labels(workflow=workflow, status=status).observe(duration)


def worker_started(name, pool_size):
    if not enabled:
        return
    _worker_pool_size.labels(runner=name).set(pool_size)
    _worker_started_at.labels(runner=name).set(time.time())


def set_inflight(name, count):
    if not enabled:
        return
    _worker_inflight.labels(runner=name).set(count)


def record_claims(name, count):
    if not enabled or not count:
        return
    _worker_claims.labels(runner=name).inc(count)


def record_requeues(name, count):
    if not enabled or not count:
        return
    _worker_requeues.labels(runner=name).inc(count)


def update_queue_gauges():
    """Refresh the queue-depth gauges from the database."""
    if not enabled:
        return
    from django.db.models import Count, Min
    from django.utils import timezone

    from everystep.models import Workflow

    scheduled = Workflow.objects.filter(status=Workflow.Status.SCHEDULED).aggregate(
        count=Count("id"), oldest=Min("created_at")
    )
    _workflows_pending.set(scheduled["count"])
    if scheduled["oldest"] is None:
        _workflows_oldest_pending_age.set(0)
    else:
        _workflows_oldest_pending_age.set(
            (timezone.now() - scheduled["oldest"]).total_seconds()
        )
    _workflows_running.set(
        Workflow.objects.filter(status=Workflow.Status.RUNNING).count()
    )
    _workflows_blocked.set(
        Workflow.objects.filter(status=Workflow.Status.BLOCKED).count()
    )


def render_latest():
    """Refresh the queue gauges and render the default registry."""
    if not enabled:
        return b""
    update_queue_gauges()
    return prometheus_client.generate_latest()


def start_http_server(port, addr):
    """Serve the default registry in a daemon thread. Returns the server.

    Raises RuntimeError if the metrics extra is not installed.
    """
    if not enabled:
        raise RuntimeError(
            "prometheus metrics are not available; install the metrics extra "
            'with pip install "everystep[metrics]"'
        )
    server, _thread = prometheus_client.start_http_server(port, addr)
    return server
