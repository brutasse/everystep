# Parallel execution

`parallel(*branches, everystep_id=None)` runs zero-arg callables concurrently and
returns their results in branch order.

```python
ip, _sg = parallel(
    lambda: attach_ip(vm_id),
    lambda: setup_security_group(vm_id, args["name"]),
)
```

A branch is:

- a single zero-arg callable — usually a lambda calling one `@step`, or a
  `@step` function itself; or
- a list of zero-arg callables, a **sequence branch**: its steps run in
  order within the branch, and the branch returns a **tuple** of their
  results. A sequence branch runs concurrently with its siblings.

Each branch runs on its own thread (one pool thread per branch); the fork
waits for all of them. Branch steps take ids under the fork's id:
`<fork>.<branch index>.<step>`, e.g. `fanout.0.1`, or `fanout.0.attach-ip`
when the branch step is named. Pass `everystep_id` to the fork for a stable fork
id — see [identity](identity.md).

## Errors

- **All branches succeed** → `parallel` returns a tuple of the results.
- **Exactly one branch fails** → that branch's exception is re-raised as is.
- **Several branches fail** → an `ExceptionGroup` is raised containing all
  of them.
- **`Terminal` (or a `DrainOrphan` during a drain) in any branch** → that
  exception is raised ahead of any `ExceptionGroup`, so a deliberate stop is
  never wrapped. See [errors](errors.md#stopping-a-workflow-terminal).

Branches are independent: when one fails, the healthy branches still run to
completion and are **recorded** — they will be served from the store on the
next replay and will not re-execute.

## Data between branches

Branches run on different threads and must not share Python objects. Pass
data **in** through serializable arguments and get it **out** through the
return tuple:

```python
enriched, ranked = parallel(
    lambda: enrich_order(order),
    lambda: rank_order(order),
)
return merge(enriched, ranked)
```

Do not read a sibling branch's outcome from
`context.current().outcomes`: completion order is not guaranteed. Read
sibling values from `parallel`'s return tuple instead. Steps from an earlier
`parallel` or earlier sequential steps are safe to read — see
[reading results](results.md).

## Outside a workflow

Called outside a running workflow, `parallel()` still runs its branches
concurrently on a thread pool, and the steps inside run as plain functions
with no recording.
