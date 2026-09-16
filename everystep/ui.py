"""The everystep UI page: a single self-contained HTML document.

Inline CSS and JS, no external assets. The view replaces __EVERYSTEP_BASE__
with the mount path so the page works under any prefix.

The run detail renders steps as an Argo-workflows-style DAG: a pure
layout function (in its own <script id="everystep-dag"> block, no DOM, so the
test suite can run it under node) positions status-colored circles and
wires fan-out/fan-in edges; the main script renders it as an SVG that
supports wheel zoom, drag panning, and click-through step details.
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>everystep</title>
<style>
:root {
  --bg: #0e1116;
  --panel: #151a23;
  --border: #232b3a;
  --text: #d8dee9;
  --dim: #8a93a6;
  --accent: #4da3ff;
  --ok: #3fb950;
  --err: #f85149;
  --warn: #d29922;
  --pend: #6e7787;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 13px/1.5 ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  display: grid;
  grid-template-rows: auto 1fr;
  grid-template-columns: minmax(430px, 45%) 1fr;
}
header {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 14px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
}
.logo { font-weight: 700; font-size: 15px; }
#run-by-id {
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 4px;
  font: inherit;
  font-size: 12px;
  padding: 2px 8px;
  width: 260px;
}
#run-by-id::placeholder { color: var(--dim); }
#run-by-id:focus { outline: none; border-color: var(--accent); }
.conn { margin-left: auto; display: flex; align-items: center; gap: 6px; color: var(--dim); }
.conn .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--pend); }
.conn.live .dot { background: var(--ok); }
.conn.polling .dot { background: var(--accent); }
.conn.down .dot { background: var(--err); }
main, #detail { overflow: auto; padding: 12px 14px; }
main { grid-column: 1; grid-row: 2; }
#detail { grid-column: 2; grid-row: 2; border-left: 1px solid var(--border); }
h2 {
  font-size: 12px;
  font-weight: 500;
  color: var(--dim);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin: 18px 0 8px;
}
h2:first-child { margin-top: 0; }
.dim { color: var(--dim); }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 5px 6px; border-bottom: 1px solid var(--border); white-space: nowrap; }
th { color: var(--dim); font-weight: 400; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; }
td.name { max-width: 0; width: 100%; overflow: hidden; text-overflow: ellipsis; }
tr.run { cursor: pointer; }
tr.run:hover td { background: #1a2130; }
tr.run.selected td { background: #1b2a45; }
.pill { padding: 0 8px; border-radius: 10px; font-size: 12px; border: 1px solid; }
.pill.completed { color: var(--ok); border-color: var(--ok); }
.pill.failed { color: var(--err); border-color: var(--err); }
.pill.running { color: var(--accent); border-color: var(--accent); }
.pill.scheduled { color: var(--dim); border-color: var(--dim); }
.pill.stopped { color: var(--warn); border-color: var(--warn); }
.pill.blocked { color: var(--warn); border-color: var(--warn); }
.pill.done { color: var(--ok); border-color: var(--ok); }
.pill.in_flight { color: var(--accent); border-color: var(--accent); }
.pill.started { color: var(--warn); border-color: var(--warn); }
.pill.pending { color: var(--dim); border-color: var(--dim); }
.bar { display: inline-block; width: 80px; height: 7px; border-radius: 3px; background: #232b3a; vertical-align: middle; }
.bar i { display: block; height: 100%; border-radius: 3px; background: var(--ok); }
#runners-list { list-style: none; margin: 0; padding: 0; }
#runners-list li { padding: 3px 0; border-bottom: 1px solid var(--border); }
.meta { display: flex; flex-wrap: wrap; gap: 4px 24px; margin-bottom: 6px; }
.meta > div { display: flex; gap: 8px; }
.meta .label { color: var(--dim); }
.banner { padding: 6px 10px; margin-bottom: 8px; border: 1px solid var(--border); border-radius: 4px; background: #131926; color: var(--dim); }
.error-box { border: 1px solid var(--err); border-radius: 4px; padding: 8px 10px; }
.error-box .type { color: var(--err); }
.counts { color: var(--dim); margin-bottom: 6px; }
.dag-legend { display: flex; align-items: center; gap: 14px; margin: 2px 0 6px; font-size: 11px; color: var(--dim); }
.dag-legend .item { display: inline-flex; align-items: center; gap: 5px; }
.dag-legend-dot { width: 10px; height: 10px; border-radius: 50%; border: 2.5px solid var(--pend); background: #10141b; }
.dag-legend-dot.done { border-color: var(--ok); }
.dag-legend-dot.failed { border-color: var(--err); }
.dag-legend-dot.in_flight { border-color: var(--accent); }
.dag-legend-dot.started { border-color: var(--warn); }
.dag-zoom { float: right; display: flex; align-items: center; gap: 4px; margin: 2px 0 6px; }
.dag-zoom button {
  background: var(--panel);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0 8px;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.dag-zoom button:hover { background: #1a2130; }
.dag-viewport {
  height: 440px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: #10141b;
  overflow: hidden;
  cursor: grab;
  touch-action: none;
  user-select: none;
}
.dag-viewport:active { cursor: grabbing; }
.dag-edges path { fill: none; stroke: #39445a; stroke-width: 1.5; }
.dag-anchor { fill: var(--dim); }
.dag-labels text { fill: var(--dim); font-size: 10px; text-anchor: middle; }
.dag-empty { fill: var(--dim); font-size: 12px; }
.dag-node { cursor: pointer; }
.dag-node .ring { fill: #10141b; stroke: var(--pend); stroke-width: 2.5; }
.dag-node .core { fill: var(--pend); }
.dag-node .halo { fill: none; stroke: var(--accent); stroke-width: 2; opacity: 0; transform-box: fill-box; transform-origin: center; }
.dag-node .nlabel { fill: var(--text); font-size: 11px; text-anchor: middle; }
.dag-node.done .ring { stroke: var(--ok); }
.dag-node.done .core { fill: var(--ok); }
.dag-node.failed .ring { stroke: var(--err); }
.dag-node.failed .core { fill: var(--err); }
.dag-node.in_flight .ring { stroke: var(--accent); }
.dag-node.in_flight .core { fill: var(--accent); }
.dag-node.in_flight .halo { animation: dag-pulse 1.2s ease-in-out infinite; }
.dag-node.started .ring { stroke: var(--warn); }
.dag-node.started .core { fill: var(--warn); }
@keyframes dag-pulse {
  0%, 100% { opacity: 0.6; transform: scale(1); }
  50% { opacity: 0; transform: scale(1.9); }
}
.dag-node:hover .ring, .dag-node.selected .ring { stroke-width: 4; }
.dag-card { margin-top: 8px; padding: 8px 10px; border: 1px solid var(--border); border-radius: 4px; background: #131926; }
.dag-card-head { display: flex; align-items: center; gap: 10px; }
.dag-card-head .sid { color: var(--dim); }
.dag-card .error-box { margin-top: 8px; }
.dag-field { margin-top: 8px; }
.dag-field .label { color: var(--dim); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 2px; }
.unmatched { list-style: none; margin: 0; padding: 0; }
.unmatched li { padding: 3px 0; border-bottom: 1px solid var(--border); color: var(--dim); }
pre {
  margin: 4px 0 6px;
  padding: 6px 8px;
  background: #10141b;
  border: 1px solid var(--border);
  border-radius: 4px;
  max-height: 280px;
  overflow: auto;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
</head>
<body>
<header>
  <span class="logo">everystep</span>
  <input id="run-by-id" placeholder="load run by id" spellcheck="false" autocomplete="off">
  <span class="conn" id="conn"><span class="dot"></span><span id="conn-label">connecting</span></span>
</header>
<main>
  <h2>runs <span id="runs-count" class="dim"></span></h2>
  <table>
    <thead>
      <tr><th>status</th><th>workflow</th><th>runner</th><th>steps</th><th></th><th>started</th><th>finished</th></tr>
    </thead>
    <tbody id="runs-body"></tbody>
  </table>
  <h2>runners</h2>
  <ul id="runners-list"></ul>
</main>
<aside id="detail"><div class="dim">select a run</div></aside>
<script id="everystep-dag">
"use strict";
// Pure DAG layout: turns a workflow's step/fork graph (or a set of recorded
// step dot-paths) into positioned nodes and edges. No DOM access, so this
// block is exercised directly under node from the test suite.

const DAG = {
  cellW: 110,
  cellH: 74,
  gap: 22,
  colGap: 34,
  forkPad: 30,
  radius: 13,
};

// Recorded dot-path steps -> items with the same shape the static graph uses.
function dagFromSteps(steps) {
  const root = { children: {}, step: null };
  for (const s of steps) {
    let node = root;
    for (const part of s.step_id.split(".")) {
      node.children[part] = node.children[part] || { children: {}, step: null };
      node = node.children[part];
    }
    node.step = s;
  }
  return dagScopeToItems(root, "");
}

function dagScopeToItems(scope, prefix) {
  const items = [];
  for (const key of dagSortedKeys(scope.children)) {
    const child = scope.children[key];
    const id = prefix + key;
    if (child.step) {
      items.push({ kind: "step", id, func: child.step.name, status: child.step.status });
    } else {
      items.push({
        kind: "fork",
        id,
        branches: dagSortedKeys(child.children).map((b) => dagScopeToItems(child.children[b], id + "." + b + ".")),
      });
    }
  }
  return items;
}

function dagSortedKeys(children) {
  return Object.keys(children).sort((a, b) => {
    const na = Number(a), nb = Number(b);
    if (!Number.isNaN(na) && !Number.isNaN(nb)) return na - nb;
    if (!Number.isNaN(na)) return -1;
    if (!Number.isNaN(nb)) return 1;
    return a.localeCompare(b);
  });
}

// items -> { nodes, edges, labels, anchors, width, height }.
// nodes: { id, func, status, x, y } with (x, y) the circle center
// edges: { from: {x, y}, to: {x, y} } from a source circle's bottom to a
//         target circle's top (fan-out at forks, fan-in after them)
// labels: { x, y, text } fork annotations
// anchors: { x, y } workflow entry/exit points, drawn when the flow starts
//           or ends in a fork (Argo-style)
function dagLayout(items) {
  const out = { nodes: [], edges: [], labels: [], anchors: [] };
  const root = dagScope(items, 0, 0, out);
  if (items.length && items[0].kind === "fork") {
    const entry = { x: root.w / 2, y: -14 };
    out.anchors.push(entry);
    for (const t of root.top) out.edges.push({ from: entry, to: t });
  }
  if (items.length && items[items.length - 1].kind === "fork") {
    const exit = { x: root.w / 2, y: root.h + 14 };
    out.anchors.push(exit);
    for (const b of root.bottom) out.edges.push({ from: b, to: exit });
  }
  return {
    nodes: out.nodes,
    edges: out.edges,
    labels: out.labels,
    anchors: out.anchors,
    width: root.w,
    height: root.h,
  };
}

function dagScope(items, x0, y0, out) {
  const rows = [];
  for (const item of items) {
    if (item.kind === "step") {
      rows.push({ kind: "step", w: DAG.cellW, h: DAG.cellH, item });
      continue;
    }
    const branches = item.branches.map((b) => dagScope(b, 0, 0, { nodes: [], edges: [], labels: [] }));
    rows.push({
      kind: "fork",
      w: branches.reduce((s, b) => s + b.w, 0) + DAG.colGap * Math.max(0, branches.length - 1),
      h: (branches.length ? Math.max(...branches.map((b) => b.h)) : 0) + DAG.forkPad * 2,
      item,
      branches,
    });
  }
  if (rows.length === 0) return { w: DAG.cellW, h: 0, top: [], bottom: [], nodes: [], edges: [], labels: [] };
  const W = Math.max(...rows.map((r) => r.w));
  const cx = x0 + W / 2;
  let y = y0;
  let prevBottom = null;
  let firstTop = null;
  let lastBottom = null;
  for (const row of rows) {
    let top, bottom;
    if (row.kind === "step") {
      const n = {
        id: row.item.id,
        func: row.item.func,
        status: row.item.status || "pending",
        x: cx,
        y: y + DAG.cellH / 2,
      };
      out.nodes.push(n);
      top = [{ x: cx, y: n.y - DAG.radius }];
      bottom = [{ x: cx, y: n.y + DAG.radius }];
    } else {
      top = [];
      bottom = [];
      out.labels.push({ x: cx, y: y + 11, text: "parallel" + (row.item.id ? " (" + row.item.id + ")" : "") });
      let bx = x0 + (W - row.w) / 2;
      for (const sub of row.branches) {
        for (const n of sub.nodes) { n.x += bx; n.y += y + DAG.forkPad; }
        for (const l of sub.labels) { l.x += bx; l.y += y + DAG.forkPad; }
        for (const e of sub.edges) {
          out.edges.push({
            from: { x: e.from.x + bx, y: e.from.y + y + DAG.forkPad },
            to: { x: e.to.x + bx, y: e.to.y + y + DAG.forkPad },
          });
        }
        out.nodes.push(...sub.nodes);
        out.labels.push(...sub.labels);
        top.push(...sub.top.map((p) => ({ x: p.x + bx, y: p.y + y + DAG.forkPad })));
        bottom.push(...sub.bottom.map((p) => ({ x: p.x + bx, y: p.y + y + DAG.forkPad })));
        bx += sub.w + DAG.colGap;
      }
    }
    if (firstTop === null) firstTop = top;
    if (prevBottom) for (const p of prevBottom) for (const t of top) out.edges.push({ from: p, to: t });
    prevBottom = bottom;
    lastBottom = bottom;
    y += row.h + DAG.gap;
  }
  return { w: W, h: y - DAG.gap - y0, top: firstTop, bottom: lastBottom, nodes: out.nodes, edges: out.edges, labels: out.labels };
}
</script>
<script>
"use strict";
const BASE = "__EVERYSTEP_BASE__";
const $ = (s) => document.querySelector(s);
const state = { runs: [], runners: [], selected: null, detail: null };
let selectedFp = null;
let es = null;
let pollTimer = null;

function setConn(mode, label) {
  $("#conn").className = "conn " + mode;
  $("#conn-label").textContent = label;
}
function h2(text) { const el = document.createElement("h2"); el.textContent = text; return el; }
function pre(text) { const el = document.createElement("pre"); el.textContent = text; return el; }
function dim(text) { const el = document.createElement("div"); el.className = "dim"; el.textContent = text; return el; }
function pill(status) { const el = document.createElement("span"); el.className = "pill " + status; el.textContent = status; return el; }
function metaItem(label, content) {
  const wrap = document.createElement("div");
  const l = document.createElement("span");
  l.className = "label";
  l.textContent = label;
  const v = document.createElement("span");
  if (content instanceof Node) v.appendChild(content);
  else v.textContent = content == null ? "—" : content;
  wrap.append(l, v);
  return wrap;
}
function td(content, cls) {
  const el = document.createElement("td");
  if (cls) el.className = cls;
  if (content instanceof Node) el.appendChild(content);
  else el.textContent = content == null ? "—" : content;
  return el;
}
function fmtTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function shortName(name) {
  const parts = String(name).split(".");
  return parts.length > 1 ? parts.slice(-2).join(".") : name;
}
function dagShortLabel(func) {
  const name = String(func).split(".").pop();
  return name.length > 14 ? name.slice(0, 13) + "…" : name;
}

// everystep's extended JSON types are stored tagged; render their value for display.
function viewTagged(v) {
  if (v.__everystep_type__ === "bytes") return v.value + " (base64)";
  if (v.__everystep_type__ === "enum") return v.value + " (" + v.type + ")";
  return String(v.value);
}
function pretty(v) {
  return JSON.stringify(v, (k, val) =>
    val && typeof val === "object" && !Array.isArray(val) && "__everystep_type__" in val && "value" in val
      ? viewTagged(val)
      : val, 2);
}

function runFp(run) {
  return [run.status, run.steps.total, run.steps.done, run.steps.failed].join("|");
}

function applySnapshot(data) {
  state.runs = data.runs;
  state.runners = data.runners;
  renderRuns();
  renderRunners();
  if (state.selected) {
    const run = data.runs.find((r) => r.id === state.selected);
    if (run && selectedFp !== null && runFp(run) !== selectedFp) refreshDetail();
  }
}

function renderRuns() {
  const tbody = $("#runs-body");
  tbody.replaceChildren();
  $("#runs-count").textContent = state.runs.length ? state.runs.length + " shown" : "";
  for (const run of state.runs) {
    const tr = document.createElement("tr");
    tr.className = "run" + (run.id === state.selected ? " selected" : "");
    tr.addEventListener("click", () => selectRun(run.id));
    const s = run.steps;
    const bar = document.createElement("span");
    bar.className = "bar";
    const fill = document.createElement("i");
    fill.style.width = (s.total ? Math.round(((s.done + s.failed) / s.total) * 100) : 0) + "%";
    if (s.failed) fill.style.background = "var(--err)";
    bar.appendChild(fill);
    tr.append(
      td(pill(run.status)),
      td(shortName(run.name), "name"),
      td(run.claimed_by),
      td(s.done + "/" + s.total),
      td(bar),
      td(fmtTime(run.created_at)),
      td(fmtTime(run.completed_at))
    );
    tr.children[1].title = run.name;
    tbody.appendChild(tr);
  }
}

function renderRunners() {
  const ul = $("#runners-list");
  ul.replaceChildren();
  if (!state.runners.length) {
    const li = document.createElement("li");
    li.className = "dim";
    li.textContent = "no runners with in-flight work";
    ul.appendChild(li);
    return;
  }
  for (const r of state.runners) {
    const li = document.createElement("li");
    li.append(r.claimed_by + " — ");
    const b = document.createElement("b");
    b.textContent = String(r.inflight);
    li.append(b, " in flight");
    ul.appendChild(li);
  }
}

async function selectRun(id) {
  state.selected = id;
  history.replaceState(null, "", "#run/" + id);
  renderRuns();
  await refreshDetail();
}

async function refreshDetail() {
  const id = state.selected;
  if (!id) return;
  let data = null;
  try {
    const res = await fetch(BASE + "api/run/" + id);
    if (res.ok) data = await res.json();
  } catch (e) {
    data = null;
  }
  // a different run was selected while this fetch was in flight
  if (state.selected !== id) return;
  state.detail = data;
  const run = state.runs.find((r) => r.id === id);
  selectedFp = run ? runFp(run) : null;
  renderDetail();
}

function renderDetail() {
  const panel = $("#detail");
  panel.replaceChildren();
  if (!state.detail) {
    panel.append(h2("run"), dim("run not found"));
    return;
  }
  const run = state.detail.run;
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.append(
    metaItem("status", pill(run.status)),
    metaItem("workflow", run.name),
    metaItem("runner", run.claimed_by),
    metaItem("started", fmtTime(run.created_at)),
    metaItem("finished", fmtTime(run.completed_at)),
    metaItem("run id", run.id)
  );
  panel.append(h2("run"), meta);
  panel.append(h2("args"), pre(pretty(run.args)));
  if (run.error) {
    const box = document.createElement("div");
    box.className = "error-box";
    const t = document.createElement("div");
    t.className = "type";
    t.textContent = run.error.type || "error";
    box.append(t, dim(run.error.message || ""));
    if (Array.isArray(run.error.args) && run.error.args.length) box.appendChild(pre(pretty(run.error.args)));
    panel.append(h2("error"), box);
  } else {
    panel.append(h2("result"), pre(pretty(run.result)));
  }
  const g = state.detail.graph;
  const byId = {};
  for (const s of state.detail.steps) byId[s.step_id] = s;
  panel.append(h2("steps"));
  if (g.supported) {
    const counts = document.createElement("div");
    counts.className = "counts";
    counts.textContent = g.total + " steps · " + g.done + " done · " + g.failed + " failed · " +
      g.in_flight + " in flight · " + g.started + " uncertain · " + g.pending + " pending";
    panel.append(counts);
  } else {
    const banner = document.createElement("div");
    banner.className = "banner";
    banner.textContent = "static graph unavailable (" + g.reason + ") — showing recorded steps";
    panel.append(banner);
  }
  renderDag(g.supported ? g.nodes : dagFromSteps(state.detail.steps), byId, panel);
  if (g.supported && g.unmatched && g.unmatched.length) {
    panel.append(h2("unmatched steps"));
    const ul = document.createElement("ul");
    ul.className = "unmatched";
    for (const id of g.unmatched) {
      const s = byId[id] || null;
      const li = document.createElement("li");
      li.textContent = id + (s ? " — " + s.name + " — " + s.status : "");
      ul.appendChild(li);
    }
    panel.appendChild(ul);
  }
}

// --- run detail: Argo-style DAG ----------------------------------------------

const dagView = { scale: 1, x: 0, y: 0 };
let dagLastSize = null;

function renderDag(items, byId, parent) {
  const layout = dagLayout(items);
  const NS = "http://www.w3.org/2000/svg";

  const wrap = document.createElement("div");
  const viewport = document.createElement("div");
  viewport.className = "dag-viewport";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("width", "100%");
  svg.setAttribute("height", "100%");
  const defs = document.createElementNS(NS, "defs");
  const marker = document.createElementNS(NS, "marker");
  marker.setAttribute("id", "dag-arrow");
  marker.setAttribute("viewBox", "0 0 10 10");
  marker.setAttribute("refX", "7");
  marker.setAttribute("refY", "5");
  marker.setAttribute("markerWidth", "6.5");
  marker.setAttribute("markerHeight", "6.5");
  marker.setAttribute("orient", "auto-start-reverse");
  const arrow = document.createElementNS(NS, "path");
  arrow.setAttribute("d", "M 0 1 L 8 5 L 0 9 z");
  marker.appendChild(arrow);
  defs.appendChild(marker);
  const world = document.createElementNS(NS, "g");
  const gEdges = document.createElementNS(NS, "g");
  gEdges.setAttribute("class", "dag-edges");
  const gLabels = document.createElementNS(NS, "g");
  gLabels.setAttribute("class", "dag-labels");
  const gNodes = document.createElementNS(NS, "g");
  gNodes.setAttribute("class", "dag-nodes");
  for (const a of layout.anchors) {
    const c = document.createElementNS(NS, "circle");
    c.setAttribute("class", "dag-anchor");
    c.setAttribute("cx", a.x);
    c.setAttribute("cy", a.y);
    c.setAttribute("r", 3.5);
    gEdges.appendChild(c);
  }
  for (const e of layout.edges) {
    const p = document.createElementNS(NS, "path");
    const dy = Math.max(18, (e.to.y - e.from.y) / 2);
    p.setAttribute("d", "M " + e.from.x + " " + e.from.y +
      " C " + e.from.x + " " + (e.from.y + dy) +
      ", " + e.to.x + " " + (e.to.y - dy) +
      ", " + e.to.x + " " + e.to.y);
    p.setAttribute("marker-end", "url(#dag-arrow)");
    gEdges.appendChild(p);
  }
  for (const l of layout.labels) {
    const t = document.createElementNS(NS, "text");
    t.setAttribute("x", l.x);
    t.setAttribute("y", l.y);
    t.textContent = l.text;
    gLabels.appendChild(t);
  }
  if (!layout.nodes.length) {
    const t = document.createElementNS(NS, "text");
    t.setAttribute("class", "dag-empty");
    t.setAttribute("x", 16);
    t.setAttribute("y", 26);
    t.textContent = "no steps recorded yet";
    gLabels.appendChild(t);
  }
  for (const n of layout.nodes) {
    const g = document.createElementNS(NS, "g");
    g.setAttribute("class", "dag-node " + n.status);
    g.setAttribute("transform", "translate(" + n.x + " " + n.y + ")");
    const title = document.createElementNS(NS, "title");
    title.textContent = n.id + " · " + n.func + " · " + n.status;
    const halo = document.createElementNS(NS, "circle");
    halo.setAttribute("class", "halo");
    halo.setAttribute("r", DAG.radius);
    const ring = document.createElementNS(NS, "circle");
    ring.setAttribute("class", "ring");
    ring.setAttribute("r", DAG.radius);
    const core = document.createElementNS(NS, "circle");
    core.setAttribute("class", "core");
    core.setAttribute("r", 5);
    const label = document.createElementNS(NS, "text");
    label.setAttribute("class", "nlabel");
    label.setAttribute("y", DAG.radius + 17);
    label.textContent = dagShortLabel(n.func);
    g.append(title, halo, ring, core, label);
    g.addEventListener("click", (ev) => { ev.stopPropagation(); selectDagNode(n, g); });
    gNodes.appendChild(g);
  }
  world.append(gEdges, gLabels, gNodes);
  svg.append(defs, world);
  viewport.appendChild(svg);

  const legend = document.createElement("div");
  legend.className = "dag-legend";
  for (const [status, label] of [["done", "done"], ["failed", "failed"], ["in_flight", "in flight"], ["started", "uncertain"], ["pending", "pending"]]) {
    const item = document.createElement("span");
    item.className = "item";
    const dot = document.createElement("span");
    dot.className = "dag-legend-dot " + status;
    const text = document.createElement("span");
    text.textContent = label;
    item.append(dot, text);
    legend.appendChild(item);
  }
  const zoom = document.createElement("span");
  zoom.className = "dag-zoom";
  const pct = document.createElement("span");
  pct.className = "dim";
  const mkBtn = (text, title, fn) => {
    const b = document.createElement("button");
    b.textContent = text;
    b.title = title;
    b.addEventListener("click", fn);
    return b;
  };
  zoom.append(
    mkBtn("+", "zoom in", () => dagZoomAt(1.25, null)),
    mkBtn("−", "zoom out", () => dagZoomAt(1 / 1.25, null)),
    mkBtn("fit", "fit to view", () => dagFitViewport()),
    pct
  );
  wrap.append(legend, zoom, viewport);

  const card = document.createElement("div");
  card.className = "dag-card dim";
  card.textContent = "click a node to inspect it";
  wrap.appendChild(card);
  parent.appendChild(wrap);

  function selectDagNode(n, g) {
    for (const el of gNodes.querySelectorAll(".dag-node")) el.classList.remove("selected");
    g.classList.add("selected");
    card.replaceChildren();
    card.className = "dag-card";
    const head = document.createElement("div");
    head.className = "dag-card-head";
    const sid = document.createElement("span");
    sid.className = "sid";
    sid.textContent = n.id;
    const fn = document.createElement("span");
    fn.textContent = n.func;
    fn.title = n.func;
    head.append(sid, fn, pill(n.status));
    card.appendChild(head);
    const step = byId[n.id];
    if (!step) {
      card.appendChild(dim("not started yet"));
      return;
    }
    const hasArgs = (Array.isArray(step.args) && step.args.length) ||
      (step.kwargs && Object.keys(step.kwargs).length);
    if (hasArgs) card.appendChild(dagField("args", Array.isArray(step.args) && step.args.length ? step.args : step.kwargs));
    if (step.result != null) card.appendChild(dagField("result", step.result));
    if (step.error) {
      const box = document.createElement("div");
      box.className = "error-box";
      if (step.error.type) {
        const t = document.createElement("div");
        t.className = "type";
        t.textContent = step.error.type;
        box.appendChild(t);
      }
      box.appendChild(pre(step.error.message || pretty(step.error)));
      if (Array.isArray(step.error.args) && step.error.args.length) box.appendChild(pre(pretty(step.error.args)));
      card.appendChild(box);
    }
    if (step.status === "started") {
      if (state.detail.run.status === "blocked") {
        card.appendChild(dim(
          "started but unrecorded — the effect may have happened. " +
          "Resolve the run with: python manage.py everystep_resolve_step " +
          state.detail.run.id + " " + n.id
        ));
      } else {
        card.appendChild(dim("effect in flight — outcome not recorded yet."));
      }
    }
  }
  function dagField(label, value) {
    const div = document.createElement("div");
    div.className = "dag-field";
    const l = document.createElement("div");
    l.className = "label";
    l.textContent = label;
    div.append(l, pre(pretty(value)));
    return div;
  }

  // --- view transform: wheel zoom (anchored on cursor), drag pan, fit ---
  function applyView() {
    world.setAttribute("transform", "translate(" + dagView.x + " " + dagView.y + ") scale(" + dagView.scale + ")");
    pct.textContent = Math.round(dagView.scale * 100) + "%";
  }
  const clampScale = (s) => Math.min(3, Math.max(0.15, s));
  function dagZoomAt(factor, anchor) {
    const rect = viewport.getBoundingClientRect();
    const ax = anchor ? anchor.x : rect.width / 2;
    const ay = anchor ? anchor.y : rect.height / 2;
    const ns = clampScale(dagView.scale * factor);
    dagView.x = ax - (ax - dagView.x) * (ns / dagView.scale);
    dagView.y = ay - (ay - dagView.y) * (ns / dagView.scale);
    dagView.scale = ns;
    applyView();
  }
  function dagFitViewport() {
    const rect = viewport.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const s = clampScale(Math.min(
      (rect.width - 28) / Math.max(60, layout.width),
      (rect.height - 28) / Math.max(40, layout.height),
      1.25
    ));
    dagView.scale = s;
    dagView.x = (rect.width - layout.width * s) / 2;
    dagView.y = (rect.height - layout.height * s) / 2;
    applyView();
  }
  viewport.addEventListener("wheel", (e) => {
    e.preventDefault();
    const rect = viewport.getBoundingClientRect();
    dagZoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15, { x: e.clientX - rect.left, y: e.clientY - rect.top });
  }, { passive: false });
  let drag = null;
  viewport.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".dag-node")) return;
    drag = { sx: e.clientX, sy: e.clientY, ox: dagView.x, oy: dagView.y };
    viewport.setPointerCapture(e.pointerId);
  });
  viewport.addEventListener("pointermove", (e) => {
    if (!drag) return;
    dagView.x = drag.ox + (e.clientX - drag.sx);
    dagView.y = drag.oy + (e.clientY - drag.sy);
    applyView();
  });
  viewport.addEventListener("pointerup", () => { drag = null; });
  viewport.addEventListener("pointercancel", () => { drag = null; });

  const size = layout.width + "x" + layout.height;
  if (size === dagLastSize) applyView();
  else dagFitViewport();
  dagLastSize = size;
}

async function loadRuns() {
  try {
    const res = await fetch(BASE + "api/runs");
    applySnapshot(await res.json());
  } catch (e) {
    setConn("down", "disconnected");
  }
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

function startPolling() {
  setConn("polling", "polling");
  stopPolling();
  loadRuns();
  pollTimer = setInterval(loadRuns, 3000);
}

function connect() {
  stopPolling();
  setConn("", "connecting");
  es = new EventSource(BASE + "api/stream");
  es.onopen = () => setConn("live", "live");
  es.onmessage = (ev) => {
    try { applySnapshot(JSON.parse(ev.data)); } catch (e) { /* ignore */ }
  };
  es.onerror = () => {
    if (es) { es.close(); es = null; }
    startPolling();
  };
}

async function init() {
  $("#run-by-id").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const id = e.target.value.trim();
    e.target.value = "";
    if (id) selectRun(id);
  });
  await loadRuns();
  const m = location.hash.match(/#run\/([\w-]+)/);
  if (m) await selectRun(m[1]);
  connect();
  window.addEventListener("hashchange", () => {
    const h = location.hash.match(/#run\/([\w-]+)/);
    if (h && h[1] !== state.selected) selectRun(h[1]);
  });
}
init();
</script>
</body>
</html>
"""
