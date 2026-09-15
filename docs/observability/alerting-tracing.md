# Alerting and tracing

## Sentry

If `sentry-sdk` is installed and initialized in the host application
(`sentry_sdk.init(dsn=...)`), every **unhandled exception that fails a
workflow run** is reported to Sentry, with the workflow's name and id
attached under a `everystep` context:

```
pip install "everystep[sentry]"
```

Failures are reported from both places a run can die: inside the runner
(exceptions that escape the workflow body) and outside it (engine-level
crashes while processing a claimed run).

Never reported:

- control-flow exceptions — simulated crashes and drain requeues;
- workflows a step stopped with `Terminal` — a deliberate stop is not a
  failure.

Without `sentry-sdk`, reporting is a no-op. everystep never initializes Sentry
itself; the host application owns the SDK.

## OpenTelemetry

With the extra installed, every workflow run is emitted as an OpenTelemetry
trace on the global tracer `everystep`:

```
pip install "everystep[otel]"
```

The span model:

- **One span per run**, named after the workflow, carrying
  `everystep.workflow.id` and `everystep.workflow.status` — `completed`, `failed`,
  `stopped`, or `running` when the run was requeued for the next claimer
  (drain) or left by a crash.
- **One child span per step actually executed**, named after the step,
  carrying `everystep.step.id`. Steps served from the store on a replay are not
  re-traced.
- **A `parallel` fork gets a span of its own**; the branch steps nest under
  it, across the worker threads (the parent's span context is captured and
  re-attached on each branch thread).
- A failed run or step ends its span in **error** with the exception
  recorded. A `Terminal` stop is not an error.

everystep only creates spans. The host application owns the `TracerProvider` and
its exporters (for example OTLP), exactly as it owns Sentry. Without the
`otel` extra, all span calls are no-ops.
