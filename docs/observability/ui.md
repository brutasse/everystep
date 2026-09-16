# UI

everystep ships a self-contained single-page UI — inline CSS and JS, no CDN
assets — that shows workflow runs, live progress, step outputs, and the
runners that hold work. Mount it in your application:

```python
# urls.py
from django.urls import include, path

urlpatterns = [
    path("everystep/", include("everystep.urls")),
]
```

## Routes

| Route | What it is |
| --- | --- |
| `/` | The UI page. |
| `/api/runs` | Recent runs as JSON; `?status=` filters by run status. |
| `/api/run/<id>` | One run: the annotated step graph, plus the recorded steps with their args, results and errors. |
| `/api/stream` | Server-Sent Events: the runs snapshot, re-sent on every change to the database. |

The UI page calls `/api/stream` for live updates: the server re-sends the
full runs snapshot whenever it changes (checked once a second, with a ping
every 15 idle seconds to keep proxies from closing the connection). Clients
whose proxies do not stream SSE fall back to polling `/api/runs`.

## What you see

**Runs** — the 200 most recent, newest first, each with its name, status
(`scheduled`, `running`, `completed`, `failed`, `stopped`, `blocked`), the
claiming runner, and a step progress count. **Runners** are the distinct
`claimed_by` values of running workflows with their in-flight counts; an
idle runner has nothing in flight and does not appear.

**One run** — the run's arguments, result or error, and its step graph with
every recorded step: id, function, args, kwargs, result, error.

Any run can be opened directly by id: paste it into the input in the
header, or load the page with a `#run/<id>` fragment — including runs
older than the 200 shown in the list.

## The step graph

For a workflow whose body is a straight sequence of steps and `parallel`
forks, the UI renders the **intended structure of the workflow** — parsed
from the source via the AST, reproducing the exact step ids the runtime
assigns — as a top-to-bottom DAG in the style of Argo Workflows: each step
is a node, each `parallel` a fan-out / fan-in, with the step's live status
as the node color:

- recorded steps show their outcome (`done`, `failed`);
- a step started by an [unsafe-to-repeat](../guides/side-effects.md#unsafe-to-repeat)
  mark but never recorded shows **uncertain** — its node card carries the
  `everystep_resolve_step` command that resolves the run;
- the in-flight step of a running workflow is shown **in flight** (pulsing);
- the rest are `pending`.

Zoom with the mouse wheel, pan by dragging, refit with the `+` / `−` /
`fit` buttons. Clicking a node opens the step's id, function, args,
kwargs, result, and error below the canvas.

Bodies the parser cannot prove — control flow, loops, dynamic step ids,
calls through local names — fall back to a **flat view** built from the
recorded step ids alone, drawn through the same layout, with the reason
for the fallback shown. The parser inlines plain (non-step) function
calls, so helper functions that just call steps are still drawn in place.

## Security

Like the metrics endpoint, the UI is **unauthenticated**: it exposes run
arguments, results, and errors. Keep the mount behind network segmentation
or your own authentication.
