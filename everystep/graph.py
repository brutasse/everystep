"""Static step graph for workflow bodies, parsed from source via ast.

The parser reproduces the exact step ids the runtime assigns (see
Context.next_id), so the static tree overlays one-to-one on the Step
table: a straight body is a sequence of steps and `parallel` forks, and a
fork's branches are the `parallel()` arguments, each with its own scope.

Calls to plain (non-step) functions are inlined: their step calls consume
positions in the calling scope, in evaluation order. Bodies the parser
cannot prove — control flow, loops, dynamic names, indirect calls — yield
None with a reason, so callers fall back to a view built from the recorded
dotpaths alone.
"""

import ast
import builtins
import copy
import inspect
import itertools
import textwrap

from everystep import api
from everystep.registry import name_of, registry

_CACHE = {}

_MISSING = object()


class _Unsupported(Exception):
    """The body is not a straight sequence of steps and forks."""


def build_graph(workflow_name):
    """Parse a workflow body into a static step graph.

    Returns (graph, reason): `graph` is a list of node dicts —
    `{"kind": "step", "id", "func"}` or
    `{"kind": "fork", "id", "branches": [[node, ...], ...]}` — or None in
    flat mode, in which case `reason` explains why.
    """
    try:
        func = registry.resolve(workflow_name)
    except LookupError:
        return None, f"workflow {workflow_name!r} is not resolvable"
    func = _unwrap(func)
    try:
        source = _function_source(func)
    except (OSError, TypeError):
        return None, "workflow source is not available"
    key = (workflow_name, source)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    try:
        tree = ast.parse(source)
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
            raise _Unsupported("workflow source is not a single function definition")
        items = _parse_scope(tree.body[0].body, _name_scope(func), "", itertools.count(1))
    except _Unsupported as exc:
        _CACHE[key] = (None, str(exc))
        return _CACHE[key]
    except SyntaxError:
        _CACHE[key] = (None, "workflow source does not parse")
        return _CACHE[key]
    _CACHE[key] = (items, None)
    return _CACHE[key]


def step_count(graph):
    """Number of step nodes in a static graph."""
    count = 0
    for node in graph:
        count += 1 if node["kind"] == "step" else sum(
            step_count(branch) for branch in node["branches"]
        )
    return count


def annotate(graph, steps, running):
    """Overlay recorded step statuses on a static graph.

    `steps` maps step id to its recorded status ("done", "failed" or
    "started").
    Returns (annotated, summary): a copy of the graph with a "status" on
    every step node ("done", "failed", "in_flight", "started" or "pending"),
    and a summary with total/done/failed/in_flight/started/pending/unmatched.
    """
    annotated = copy.deepcopy(graph)
    summary: dict = {
        "total": 0,
        "done": 0,
        "failed": 0,
        "in_flight": 0,
        "started": 0,
        "pending": 0,
    }
    static_ids = []

    def count(items):
        for node in items:
            if node["kind"] == "step":
                summary["total"] += 1
                static_ids.append(node["id"])
            else:
                for branch in node["branches"]:
                    count(branch)

    def walk(items, active):
        # active: the run is running and this scope is reachable, so the
        # first unrecorded step here is the one in flight. A recorded
        # `started` row of a marked step is the same thing while the run is
        # still running (the step is executing); it only reads as `started`
        # (uncertain) once the run has stopped on it.
        reachable = True
        settled = True
        for node in items:
            if node["kind"] == "step":
                status = steps.get(node["id"])
                if status is not None:
                    if status == "started" and active:
                        node["status"] = "in_flight"
                        summary["in_flight"] += 1
                    else:
                        node["status"] = status
                        summary[status] += 1
                    # A started step has no settled outcome yet: nothing after
                    # it has run.
                    ok = status != "started"
                elif reachable and active:
                    node["status"] = "in_flight"
                    summary["in_flight"] += 1
                    ok = False
                else:
                    node["status"] = "pending"
                    summary["pending"] += 1
                    ok = False
            else:
                ok = True
                for branch in node["branches"]:
                    ok = walk(branch, active and reachable) and ok
            reachable = reachable and ok
            settled = settled and ok
        return settled

    count(annotated)
    walk(annotated, running)
    summary["unmatched"] = sorted(set(steps) - set(static_ids))
    return annotated, summary


def _unwrap(func):
    for _ in range(20):
        inner = getattr(func, "__wrapped__", None)
        if inner is None:
            return func
        func = inner
    return func


def _parse_scope(statements, globals_map, prefix, counter):
    items = []
    for stmt in statements:
        if not isinstance(
            stmt, (ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Return, ast.Pass)
        ):
            raise _Unsupported(f"unsupported statement {type(stmt).__name__} in workflow body")
        value = getattr(stmt, "value", None)
        if value is None:
            continue
        _collect(value, globals_map, prefix, counter, items)
    return items


def _collect(node, globals_map, prefix, counter, items):
    """Append the step and fork nodes contained in `node`, in the order the
    runtime executes them."""
    if isinstance(node, ast.Call):
        _collect(node.func, globals_map, prefix, counter, items)
        kind, resolved = _classify(node, globals_map)
        if kind == "fork":
            for kw in node.keywords:
                if kw.arg is None or kw.arg != "everystep_id":
                    raise _Unsupported("unexpected keyword in parallel() call")
            items.append(_fork_node(node, prefix, counter, globals_map))
            return
        for arg in node.args:
            _collect(arg, globals_map, prefix, counter, items)
        for kw in node.keywords:
            if kw.arg is None:
                raise _Unsupported("**kwargs in a call in the workflow body")
            _collect(kw.value, globals_map, prefix, counter, items)
        if kind == "step":
            items.append(_step_node(node, resolved, prefix, counter))
        elif resolved is not None and hasattr(resolved, "__code__"):
            # A plain function: its step calls run in this scope, so inline
            # them at this position.
            items.extend(_parse_function_body(resolved, prefix, counter))
        # Anything else is plain deterministic code with no everystep calls.
    elif isinstance(node, (ast.Attribute, ast.Starred)):
        _collect(node.value, globals_map, prefix, counter, items)
    elif isinstance(node, ast.Subscript):
        _collect(node.value, globals_map, prefix, counter, items)
        _collect(node.slice, globals_map, prefix, counter, items)
    elif isinstance(node, ast.BinOp):
        _collect(node.left, globals_map, prefix, counter, items)
        _collect(node.right, globals_map, prefix, counter, items)
    elif isinstance(node, ast.UnaryOp):
        _collect(node.operand, globals_map, prefix, counter, items)
    elif isinstance(node, ast.BoolOp):
        for value in node.values:
            _collect(value, globals_map, prefix, counter, items)
    elif isinstance(node, ast.Compare):
        _collect(node.left, globals_map, prefix, counter, items)
        for comparator in node.comparators:
            _collect(comparator, globals_map, prefix, counter, items)
    elif isinstance(node, ast.IfExp):
        _collect(node.test, globals_map, prefix, counter, items)
        _collect(node.body, globals_map, prefix, counter, items)
        _collect(node.orelse, globals_map, prefix, counter, items)
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for elt in node.elts:
            _collect(elt, globals_map, prefix, counter, items)
    elif isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            if key is not None:
                _collect(key, globals_map, prefix, counter, items)
            _collect(value, globals_map, prefix, counter, items)
    elif isinstance(node, ast.JoinedStr):
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                _collect(value.value, globals_map, prefix, counter, items)
    elif isinstance(node, (ast.Name, ast.Constant)):
        pass
    else:
        raise _Unsupported(f"unsupported expression {type(node).__name__} in the workflow body")


def _name_scope(func):
    """The names visible in `func`: its module globals plus its closure
    free variables, resolved through the current cell contents."""
    scope = dict(func.__globals__)
    code = getattr(func, "__code__", None)
    closure = getattr(func, "__closure__", None)
    if code is not None and closure is not None:
        for name, cell in zip(code.co_freevars, closure):
            try:
                scope[name] = cell.cell_contents
            except ValueError:
                pass  # empty cell: the variable is unbound
    return scope


def _classify(call, globals_map):
    """Resolve a call's function object; return (kind, resolved) with kind
    "step", "fork" or None for plain deterministic code. A step or fork can
    only be reached through a module-level name (or an attribute of one), so
    method calls on other objects are plain code."""
    func = call.func
    if isinstance(func, ast.Name):
        if func.id in globals_map:
            obj = globals_map[func.id]
        elif hasattr(builtins, func.id):
            obj = getattr(builtins, func.id)
        else:
            raise _Unsupported(f"call to local {func.id!r} in the workflow body")
    elif isinstance(func, ast.Attribute):
        if not isinstance(func.value, ast.Name) or func.value.id not in globals_map:
            return None, None
        obj = getattr(globals_map[func.value.id], func.attr, _MISSING)
        if obj is _MISSING:
            return None, None
    else:
        raise _Unsupported("indirect call in the workflow body")
    if getattr(obj, "__everystep__", None) == "step":
        return "step", obj
    if obj is api.parallel:
        return "fork", obj
    return None, obj


def _everystep_id(call):
    for kw in call.keywords:
        if kw.arg == "everystep_id":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
            raise _Unsupported("dynamic everystep_id in the workflow body")
    return None


def _step_node(call, resolved, prefix, counter):
    tick = next(counter)
    name = _everystep_id(call) if call is not None else None
    return {"kind": "step", "id": prefix + (name or str(tick)), "func": name_of(resolved)}


def _fork_node(call, prefix, counter, globals_map):
    tick = next(counter)
    fork_id = prefix + (_everystep_id(call) or str(tick))
    branches = []
    for index, arg in enumerate(call.args):
        branches.append(_parse_branch(arg, f"{fork_id}.{index}.", globals_map))
    return {"kind": "fork", "id": fork_id, "branches": branches}


def _parse_branch(arg, prefix, globals_map):
    """Parse one `parallel()` argument into a branch scope. All branch
    elements share one counter, as they run in the same runtime context."""
    counter = itertools.count(1)
    elements = arg.elts if isinstance(arg, ast.List) else [arg]
    items = []
    for element in elements:
        _branch_element(element, globals_map, prefix, counter, items)
    return items


def _branch_element(element, globals_map, prefix, counter, items):
    if isinstance(element, ast.Name):
        if element.id not in globals_map:
            raise _Unsupported(f"branch refers to local {element.id!r}")
        obj = globals_map[element.id]
        if getattr(obj, "__everystep__", None) == "step":
            items.append(_step_node(None, obj, prefix, counter))
            return
        if hasattr(obj, "__code__"):
            items.extend(_parse_function_body(obj, prefix, counter))
            return
        raise _Unsupported("branch is not a step or a plain function")
    if not isinstance(element, ast.Lambda):
        raise _Unsupported(f"branch element {type(element).__name__}")
    _collect(element.body, globals_map, prefix, counter, items)


def _parse_function_body(func, prefix, counter):
    """Parse a plain (non-step) function's body into items for the calling
    scope. A body with control flow is inlined as opaque if it cannot call
    any step or fork; otherwise _Unsupported is raised (flat mode)."""
    func = _unwrap(func)
    body, globals_map = _function_body(func)
    try:
        return _parse_scope(body, globals_map, prefix, counter)
    except _Unsupported:
        if _calls_everystep(body, globals_map, set()):
            raise
        return []


def _function_source(func):
    # getsource keeps the indentation of nested and class-body functions;
    # dedent before parsing so all of them work.
    return textwrap.dedent(inspect.getsource(func))


def _function_body(func):
    try:
        tree = ast.parse(_function_source(func))
    except (OSError, TypeError, SyntaxError):
        raise _Unsupported("source for a called function is not available")
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise _Unsupported("called function source is not a function definition")
    return tree.body[0].body, _name_scope(func)


def _calls_everystep(body, globals_map, seen):
    """Best effort: can this source execute a step or fork call, including
    under control flow or via other functions? Only module-level names and
    module attributes are considered reachable step identities (matching how
    @step functions are registered), so calls through locals or methods are
    treated as plain code."""
    for node in ast.walk(ast.Module(body=list(body), type_ignores=[])):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in globals_map:
            obj = globals_map[func.id]
        elif (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in globals_map
        ):
            obj = getattr(globals_map[func.value.id], func.attr, None)
        else:
            continue
        if getattr(obj, "__everystep__", None) == "step" or obj is api.parallel:
            return True
        if hasattr(obj, "__code__"):
            target = _unwrap(obj)
            key = (getattr(target, "__module__", ""), getattr(target, "__qualname__", ""))
            if key in seen:
                continue
            seen.add(key)
            try:
                sub_body, sub_globals = _function_body(target)
            except _Unsupported:
                return True
            if _calls_everystep(sub_body, sub_globals, seen):
                return True
    return False
