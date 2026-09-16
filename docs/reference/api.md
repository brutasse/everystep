# API reference

The public API is small on purpose. Everything you need to write workflows
is four callables and one exception; the rest is what you use to observe
runs and steps. Rendered from the source.

## Core

The four core callables live in `everystep.api` and are re-exported from the
`everystep` package (lazily, so importing `everystep` does not import Django). The
blocks below document them at their definition site.

::: everystep.api.workflow

::: everystep.api.step

::: everystep.api.parallel

::: everystep.api.schedule

::: everystep.Terminal

## Execution context

::: everystep.context.current

::: everystep.context.Context

## Exceptions

::: everystep.errors.EverystepError

::: everystep.errors.WorkflowCodeError

::: everystep.errors.EffectUncertain

::: everystep.errors.StepFailure

::: everystep.errors.SimulatedCrash

::: everystep.errors.DrainOrphan

## Stored exceptions

::: everystep.serde.encode_exception

::: everystep.serde.decode_exception

## Worker

::: everystep.worker.Worker
