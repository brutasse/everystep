import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

import pytest
from django.test import Client

import everystep.views as views
from everystep import runner
from everystep import schedule, step, ui, workflow
from everystep.errors import SimulatedCrash
from everystep.models import Workflow
from everystep.runner import execute
from tests.helpers import crash_on, re_claim, run_to_completion

pytestmark = pytest.mark.django_db(transaction=True)


@step
def u_a():
    return "a"


@step
def u_b(x):
    return f"b{x}"


@step(unsafe_to_repeat=True)
def u_risky():
    return "r"


@workflow
def u_blocked(args):
    u_a()
    return u_risky()


@workflow
def u_chain(args):
    return u_a() + u_b(args.get("x", ""))


@workflow
def u_loop(args):
    return "".join(u_b(i) for i in range(2))


def _claim(wf):
    """Claim a specific workflow by id; other tests leave claimable rows
    behind (the suite runs without per-test rollback)."""
    updated = Workflow.objects.filter(id=wf.id, status=Workflow.Status.SCHEDULED).update(
        status=Workflow.Status.RUNNING, claimed_by="test-worker"
    )
    assert updated == 1
    wf.refresh_from_db()
    return wf


def test_ui_page():
    resp = Client().get("/everystep/")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/html")
    html = resp.content.decode()
    assert "__EVERYSTEP_BASE__" not in html
    assert 'const BASE = "/everystep/";' in html
    assert 'id="run-by-id"' in html


def test_runs_list():
    done = run_to_completion(u_chain, {"x": "1"})
    running = _claim(schedule(u_chain, {"x": "2"}))
    scheduled = schedule(u_chain, {"x": "3"})
    data = Client().get("/everystep/api/runs").json()
    by_id = {r["id"]: r for r in data["runs"]}
    assert by_id[str(done.id)]["status"] == Workflow.Status.COMPLETED
    assert by_id[str(done.id)]["steps"] == {"total": 2, "done": 2, "failed": 0}
    assert by_id[str(scheduled.id)]["status"] == Workflow.Status.SCHEDULED
    assert by_id[str(scheduled.id)]["steps"] == {"total": 2, "done": 0, "failed": 0}
    assert by_id[str(running.id)]["status"] == Workflow.Status.RUNNING
    assert by_id[str(running.id)]["claimed_by"] == "test-worker"
    assert any(
        r["claimed_by"] == "test-worker" and r["inflight"] >= 1 for r in data["runners"]
    )
    for r in data["runs"]:
        assert isinstance(r["created_at"], str)
        assert r["completed_at"] is None or isinstance(r["completed_at"], str)


def test_runs_list_status_filter():
    run_to_completion(u_chain, {"x": "1"})
    schedule(u_chain, {"x": "2"})
    data = Client().get("/everystep/api/runs?status=completed").json()
    assert data["runs"]
    assert all(r["status"] == Workflow.Status.COMPLETED for r in data["runs"])
    data = Client().get("/everystep/api/runs?status=nonsense").json()
    assert {r["status"] for r in data["runs"]} <= set(Workflow.Status.values)


def test_run_detail():
    run = run_to_completion(u_chain, {"x": "1"})
    data = Client().get(f"/everystep/api/run/{run.id}").json()
    assert data["run"]["status"] == Workflow.Status.COMPLETED
    assert data["run"]["args"] == [{"x": "1"}]
    assert data["run"]["result"] == "ab1"
    g = data["graph"]
    assert g["supported"] is True
    assert [n["id"] for n in g["nodes"]] == ["1", "2"]
    assert all(n["status"] == "done" for n in g["nodes"])
    assert g["total"] == 2 and g["done"] == 2
    assert g["unmatched"] == []
    assert [s["step_id"] for s in data["steps"]] == ["1", "2"]
    assert data["steps"][0]["result"] == "a"
    assert data["steps"][0]["kwargs"] == {}
    assert data["steps"][1]["args"] == ["1"]


def test_run_detail_failed_step():
    @step
    def boom():
        raise ValueError("kaput")

    @workflow
    def w_fails(args):
        u_a()
        return boom()

    wf = _claim(schedule(w_fails, {}))
    execute(wf.id)
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.FAILED
    data = Client().get(f"/everystep/api/run/{wf.id}").json()
    g = data["graph"]
    assert g["supported"] is True
    assert g["failed"] == 1
    assert data["run"]["error"]["message"] == "kaput"
    failed = [s for s in data["steps"] if s["status"] == "failed"][0]
    assert failed["error"]["message"] == "kaput"


def test_run_detail_blocked():
    wf = _claim(schedule(u_blocked, {}))
    runner.fault = crash_on("2")
    with pytest.raises(SimulatedCrash):
        execute(wf.id)
    runner.fault = None

    re_claim(wf)
    execute(wf.id)
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.BLOCKED

    data = Client().get(f"/everystep/api/run/{wf.id}").json()
    assert data["run"]["status"] == Workflow.Status.BLOCKED
    assert data["run"]["error"]["type"].endswith("EffectUncertain")
    g = data["graph"]
    assert g["supported"] is True
    assert g["done"] == 1 and g["started"] == 1
    assert [n["status"] for n in g["nodes"]] == ["done", "started"]
    assert data["steps"][0]["status"] == "done"
    assert data["steps"][1]["status"] == "started"

    by_status = Client().get("/everystep/api/runs?status=blocked").json()
    assert any(r["id"] == str(wf.id) for r in by_status["runs"])


def test_run_detail_marked_step_in_flight():
    # A marked step executing is shown in flight, not uncertain: its
    # started pre-record is the in-flight marker while the run is running.
    wf = _claim(schedule(u_blocked, {}))
    runner.fault = crash_on("2")
    with pytest.raises(SimulatedCrash):
        execute(wf.id)
    runner.fault = None

    data = Client().get(f"/everystep/api/run/{wf.id}").json()
    assert data["run"]["status"] == Workflow.Status.RUNNING
    g = data["graph"]
    assert [n["status"] for n in g["nodes"]] == ["done", "in_flight"]
    assert g["in_flight"] == 1 and g["started"] == 0
    assert data["steps"][1]["status"] == "started"


def test_run_detail_flat():
    run = run_to_completion(u_loop, {})
    data = Client().get(f"/everystep/api/run/{run.id}").json()
    g = data["graph"]
    assert g["supported"] is False
    assert g["reason"]
    assert {s["step_id"] for s in data["steps"]} == {"1", "2"}
    assert data["run"]["result"] == "b0b1"


def test_run_detail_404():
    resp = Client().get(f"/everystep/api/run/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_run_detail_beyond_list_limit(monkeypatch):
    """A run older than the list window is absent from /api/runs but still
    loads by id straight from the database."""
    monkeypatch.setattr(views, "_RUNS_LIMIT", 2)
    old = run_to_completion(u_chain, {"x": "old"})
    run_to_completion(u_chain, {"x": "mid"})
    run_to_completion(u_chain, {"x": "new"})
    list_ids = {r["id"] for r in Client().get("/everystep/api/runs").json()["runs"]}
    assert str(old.id) not in list_ids
    data = Client().get(f"/everystep/api/run/{old.id}").json()
    assert data["run"]["id"] == str(old.id)
    assert data["run"]["result"] == "abold"


def _dag_block_js():
    m = re.search(r'<script id="everystep-dag">(.*?)</script>', ui.PAGE, re.S)
    assert m, "pure DAG block missing from page"
    return m.group(1)


def _run_node(js):
    if shutil.which("node") is None:
        pytest.skip("node not available")
    fd, path = tempfile.mkstemp(suffix=".js")
    try:
        with open(fd, "w") as f:
            f.write(js)
        proc = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)
    finally:
        os.unlink(path)


def test_dag_layout():
    """The pure layout block positions nodes without overlap, wires
    fan-out/fan-in edges, and flows strictly top-down."""
    js = _dag_block_js() + r"""
const results = {};

function check(name, items, wantNodes, wantEdges) {
  const layout = dagLayout(items);
  const ids = layout.nodes.map((n) => n.id).sort();
  if (JSON.stringify(ids) !== JSON.stringify([...wantNodes].sort()))
    throw new Error(name + ": nodes " + ids.join(",") + " != " + wantNodes.join(","));
  if (layout.edges.length !== wantEdges)
    throw new Error(name + ": " + layout.edges.length + " edges, want " + wantEdges);
  const minD = 2 * DAG.radius + 2;
  for (let i = 0; i < layout.nodes.length; i++) {
    for (let j = i + 1; j < layout.nodes.length; j++) {
      const a = layout.nodes[i], b = layout.nodes[j];
      if (Math.hypot(a.x - b.x, a.y - b.y) < minD)
        throw new Error(name + ": " + a.id + " overlaps " + b.id);
    }
  }
  const anchor = (p, side) => layout.nodes.find((n) =>
    Math.abs(n.x - p.x) < 0.5 &&
    Math.abs(n.y + (side === "bottom" ? DAG.radius : -DAG.radius) - p.y) < 0.5);
  const entryExit = [];
  if (items.length && items[0].kind === "fork") entryExit.push([layout.width / 2, -14]);
  if (items.length && items[items.length - 1].kind === "fork")
    entryExit.push([layout.width / 2, layout.height + 14]);
  const isEntryExit = (p) => entryExit.some(([x, y]) => Math.abs(x - p.x) < 0.5 && Math.abs(y - p.y) < 0.5);
  if (layout.anchors.length !== entryExit.length)
    throw new Error(name + ": " + layout.anchors.length + " anchors, want " + entryExit.length);
  for (const e of layout.edges) {
    if (!isEntryExit(e.from) && !anchor(e.from, "bottom"))
      throw new Error(name + ": edge from " + JSON.stringify(e.from) + " not at a node bottom or entry");
    if (!isEntryExit(e.to) && !anchor(e.to, "top"))
      throw new Error(name + ": edge to " + JSON.stringify(e.to) + " not at a node top or exit");
    if (e.from.y >= e.to.y)
      throw new Error(name + ": edge not top-down " + JSON.stringify(e.from) + " -> " + JSON.stringify(e.to));
  }
  for (const n of layout.nodes) {
    if (n.x < -0.5 || n.x > layout.width + 0.5 || n.y < -0.5 || n.y > layout.height + 0.5)
      throw new Error(name + ": node " + n.id + " out of bounds");
  }
  results[name] = { nodes: layout.nodes.length, edges: layout.edges.length };
}

const S = (id) => ({ kind: "step", id, func: id, status: "done" });
const F = (id, branches) => ({ kind: "fork", id, branches });

check("linear", [S("1"), S("2"), S("3")], ["1", "2", "3"], 2);
check("fork", [S("1"), F("fan", [[S("fan.0.1")], [S("fan.1.1")]]), S("3")],
  ["1", "3", "fan.0.1", "fan.1.1"], 4);
check("nested",
  [S("1"),
   F("fan", [
     [S("fan.0.1"), F("fan.0.2", [[S("fan.0.2.0.1")], [S("fan.0.2.1.1")]]), S("fan.0.3")],
     [S("fan.1.1")],
   ]),
   S("4")],
  ["1", "4", "fan.0.1", "fan.0.2.0.1", "fan.0.2.1.1", "fan.0.3", "fan.1.1"], 8);
// workflow that starts and ends in a fork gets entry/exit anchors
check("forkFirst", [F("fan", [[S("fan.0.1")], [S("fan.1.1")]])], ["fan.0.1", "fan.1.1"], 4);

// recorded dot-paths (flat mode) normalize to the same item shape:
// steps 1, 2, fork in slot 3 (branch 0: two steps, branch 1: one), step 4
check("flat", dagFromSteps([
  { step_id: "1", name: "a", status: "done" },
  { step_id: "2", name: "b", status: "done" },
  { step_id: "3.0.1", name: "c", status: "done" },
  { step_id: "3.0.2", name: "d", status: "done" },
  { step_id: "3.1.1", name: "e", status: "done" },
  { step_id: "4", name: "f", status: "done" },
]), ["1", "2", "3.0.1", "3.0.2", "3.1.1", "4"], 6);

console.log(JSON.stringify(results));
"""
    assert _run_node(js) == {
        "linear": {"nodes": 3, "edges": 2},
        "fork": {"nodes": 4, "edges": 4},
        "nested": {"nodes": 7, "edges": 8},
        "forkFirst": {"nodes": 2, "edges": 4},
        "flat": {"nodes": 6, "edges": 6},
    }


def test_page_script_blocks_parse():
    """Both script blocks are syntactically valid JS."""
    if shutil.which("node") is None:
        pytest.skip("node not available")
    for pattern in (r'<script id="everystep-dag">(.*?)</script>', r'<script>\n(.*)</script>'):
        m = re.search(pattern, ui.PAGE, re.S)
        assert m, f"script block missing: {pattern}"
        fd, path = tempfile.mkstemp(suffix=".js")
        try:
            with open(fd, "w") as f:
                f.write(m.group(1))
            proc = subprocess.run(["node", "--check", path], capture_output=True, text=True, timeout=30)
            assert proc.returncode == 0, proc.stderr
        finally:
            os.unlink(path)


def _sse_json(chunk):
    text = chunk.decode()
    assert text.startswith("data: ")
    return json.loads(text[len("data:") :].strip())


def test_stream_sse(monkeypatch):
    monkeypatch.setattr(views, "_SSE_POLL_INTERVAL", 0.05)
    existing = run_to_completion(u_chain, {"x": "1"})
    resp = Client().get("/everystep/api/stream")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/event-stream")
    assert resp["X-Accel-Buffering"] == "no"
    chunks = iter(resp.streaming_content)
    first = _sse_json(next(chunks))
    assert any(r["id"] == str(existing.id) for r in first["runs"])
    new = schedule(u_chain, {"x": "2"})
    second = _sse_json(next(chunks))
    assert any(r["id"] == str(new.id) for r in second["runs"])
    resp.close()


def test_stream_shows_step_progress(monkeypatch):
    """A running workflow's step counts advance over the stream."""
    monkeypatch.setattr(views, "_SSE_POLL_INTERVAL", 0.05)
    wf = _claim(schedule(u_chain, {"x": "1"}))
    resp = Client().get("/everystep/api/stream")
    chunks = iter(resp.streaming_content)
    first = _sse_json(next(chunks))
    row = next(r for r in first["runs"] if r["id"] == str(wf.id))
    assert row["status"] == Workflow.Status.RUNNING
    assert row["steps"] == {"total": 2, "done": 0, "failed": 0}

    def _run():
        execute(wf.id)
        # Release the thread-local connection the way Worker._execute does,
        # or the test database teardown fails.
        from django.db import connections

        connections.close_all()

    thread = threading.Thread(target=_run)
    thread.start()
    final = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        data = _sse_json(next(chunks))
        row = next(r for r in data["runs"] if r["id"] == str(wf.id))
        if row["status"] == Workflow.Status.COMPLETED:
            final = row
            break
    thread.join()
    resp.close()
    assert final is not None
    assert final["steps"] == {"total": 2, "done": 2, "failed": 0}
