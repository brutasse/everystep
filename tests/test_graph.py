import pytest

from everystep import graph, parallel, schedule, step, workflow
from everystep.errors import SimulatedCrash
from everystep.models import Step, Workflow
from everystep.registry import name_of
from everystep.runner import execute
from tests.helpers import claim_next, crash_on, re_claim, run_to_completion

pytestmark = pytest.mark.django_db(transaction=True)


@step
def g_a():
    return "a"


@step
def g_b(x):
    return f"b{x}"


@step
def g_c():
    return "c"


def _recorded(run):
    return {s.step_id: s.name for s in Step.objects.filter(workflow_id=run.id)}


def _statuses(run):
    return {s.step_id: s.status for s in Step.objects.filter(workflow_id=run.id)}


def _static(g):
    out = {}

    def walk(items):
        for node in items:
            if node["kind"] == "step":
                out[node["id"]] = node["func"]
            else:
                for branch in node["branches"]:
                    walk(branch)

    walk(g)
    return out


def check_graph(wf):
    """The parser's ids must be exactly the ids the runtime records."""
    run = run_to_completion(wf, {})
    g, reason = graph.build_graph(name_of(wf))
    assert g is not None, reason
    assert _static(g) == _recorded(run)


@workflow
def seq(args):
    return g_a() + g_b(1) + g_c()


@workflow
def named(args):
    g_a(everystep_id="one")
    g_b(2, everystep_id="two")
    return g_c()


@workflow
def fork(args):
    a, b = parallel(lambda: g_a(), lambda: g_b("x"), everystep_id="fan")
    return g_c() + a + b


@workflow
def seq_branch(args):
    a, b = parallel([lambda: g_a(), lambda: g_b("q")], g_c, everystep_id="fan")
    return a + b


@workflow
def nested_fork(args):
    parallel(lambda: parallel(lambda: g_a(), lambda: g_b("z")), g_c)
    return "ok"


@workflow
def nested_calls(args):
    return g_b(g_a() + g_c())


@workflow
def multi_fork(args):
    parallel(lambda: g_a(), lambda: g_b("x"), everystep_id="fan")
    g_c()
    return parallel(lambda: g_a(), lambda: g_b("y"), everystep_id="fan2")


def build_args(name):
    return name + name


@workflow
def helper_call(args):
    x = build_args("a")
    return g_b(x)


def branch_helper():
    g_a()
    return g_b("h")


@workflow
def branch_function(args):
    a, b = parallel(branch_helper, lambda: g_c())
    return a + b


@pytest.mark.parametrize(
    "wf",
    [seq, named, fork, seq_branch, nested_fork, nested_calls, multi_fork, helper_call, branch_function],
)
def test_graph_matches_runtime(wf):
    check_graph(wf)


@workflow
def looped(args):
    return "".join(g_b(i) for i in range(2))


@workflow
def conditional(args):
    if args.get("x"):
        g_a()
    return g_c()


@workflow
def dynamic_name(args):
    return g_a(everystep_id=f"iter-{args['i']}")


@workflow
def local_call(args):
    def helper():
        return 1

    return g_b(helper())


@pytest.mark.parametrize(
    "wf",
    [
        pytest.param(looped, id="generator-expression"),
        pytest.param(conditional, id="control-flow"),
        pytest.param(dynamic_name, id="dynamic-everystep_id"),
        pytest.param(local_call, id="local-function-call"),
    ],
)
def test_flat_fallback(wf):
    g, reason = graph.build_graph(name_of(wf))
    assert g is None
    assert reason
    # The workflow itself still runs; only the static graph is unavailable.
    run = run_to_completion(wf, {"x": 1, "i": 0})
    assert run.status == Workflow.Status.COMPLETED
    assert _recorded(run)


def test_unresolvable_workflow():
    g, reason = graph.build_graph("no.such.module.workflow")
    assert g is None
    assert "not resolvable" in reason


def test_annotate_marks_in_flight_step():
    wf = schedule(seq, {})
    claim_next()
    from everystep import runner

    runner.fault = crash_on("2")
    with pytest.raises(SimulatedCrash):
        execute(wf.id)
    runner.fault = None
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.RUNNING

    g, reason = graph.build_graph(name_of(seq))
    annotated, summary = graph.annotate(g, _statuses(wf), wf.status == Workflow.Status.RUNNING)
    statuses = {node["id"]: node["status"] for node in _walk(annotated)}
    assert statuses == {"1": "done", "2": "in_flight", "3": "pending"}
    assert summary["total"] == 3
    assert summary["done"] == 1
    assert summary["in_flight"] == 1
    assert summary["pending"] == 1
    assert summary["unmatched"] == []

    re_claim(wf)
    execute(wf.id)
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.COMPLETED
    annotated, summary = graph.annotate(g, _statuses(wf), wf.status == Workflow.Status.RUNNING)
    assert summary == {
        "total": 3,
        "done": 3,
        "failed": 0,
        "in_flight": 0,
        "started": 0,
        "pending": 0,
        "unmatched": [],
    }


def test_annotate_reports_unmatched_ids():
    g, reason = graph.build_graph(name_of(seq))
    assert g is not None
    _, summary = graph.annotate(g, {"1": "done", "9": "done"}, False)
    assert summary["unmatched"] == ["9"]
    # annotate must not mutate the cached graph
    assert "status" not in g[0]


def test_annotate_fork_in_flight():
    g, reason = graph.build_graph(name_of(fork))
    assert g is not None
    # Nothing recorded, run just claimed: both branch steps in flight, the
    # trailing step still pending.
    _, summary = graph.annotate(g, {}, True)
    assert summary == {
        "total": 3,
        "done": 0,
        "failed": 0,
        "in_flight": 2,
        "started": 0,
        "pending": 1,
        "unmatched": [],
    }
    # One branch recorded: the other branch step stays in flight, the
    # trailing step still cannot start.
    annotated, summary = graph.annotate(g, {"fan.0.1": "done"}, True)
    statuses = {node["id"]: node["status"] for node in _walk(annotated)}
    assert statuses == {
        "fan.0.1": "done",
        "fan.1.1": "in_flight",
        "2": "pending",
    }
    assert summary["in_flight"] == 1
    assert summary["pending"] == 1


def _walk(items):
    for node in items:
        if node["kind"] == "step":
            yield node
        else:
            for branch in node["branches"]:
                yield from _walk(branch)
