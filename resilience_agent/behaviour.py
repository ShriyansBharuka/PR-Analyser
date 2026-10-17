"""Differential behaviour testing: same inputs on base and head, compare outputs (VERIFIED).

Inputs are RECORDED while base's own tests run (a pytest plugin wraps the target functions and pickles their
arguments), then replayed in a subprocess on base (twice, to drop non-deterministic functions) and on head.
Only "pure enough" module-level functions are replayed: no filesystem, network, clock, randomness or globals,
directly or through internal callees.

Optional AI-generated tests live ONLY in .resilience/generated_tests/<review>/ and count only if they pass on base.
"""
import ast
import json
import re
from pathlib import Path

from pydantic import BaseModel

from .review import review_env
from .verification import CommandRunner

IMPURE = {"open", "print", "input", "exec", "eval", "compile", "__import__", "globals", "os", "sys", "time", "datetime",
          "random", "socket", "subprocess", "requests", "httpx", "urllib", "aiohttp", "pathlib", "Path", "shutil",
          "tempfile", "threading", "multiprocessing", "asyncio", "logging", "uuid", "secrets", "signal", "glob", "io"}
MAX_INPUTS = 20

CAPTURE_PLUGIN = '''
import base64, functools, importlib, json, os, pickle, sys
TARGETS = json.loads(os.environ.get("RA_CAPTURE_TARGETS", "[]"))
SEEN = {}

def _wrap(name, fn):
    @functools.wraps(fn)
    def inner(*a, **k):
        bucket = SEEN.setdefault(name, [])
        if len(bucket) < %d:
            try:
                blob = base64.b64encode(pickle.dumps((a, k))).decode()
                if len(blob) < 65536 and blob not in bucket:
                    bucket.append(blob)
            except Exception:
                pass
        return fn(*a, **k)
    inner.__ra_wrapped__ = True
    return inner

def pytest_configure(config):
    for t in TARGETS:
        mod, _, attr = t.partition(":")
        try:
            m = importlib.import_module(mod)
            orig = getattr(m, attr)
        except Exception:
            continue
        w = _wrap(t, orig)
        for other in list(sys.modules.values()):  # re-exports such as pkg/__init__: from .core import f
            if getattr(other, attr, None) is orig:
                setattr(other, attr, w)

def pytest_unconfigure(config):
    with open(os.environ["RA_CAPTURE_OUT"], "w", encoding="utf-8") as f:
        json.dump(SEEN, f)
''' % MAX_INPUTS

REPLAY = '''
import base64, importlib, json, os, pickle, re, sys
sys.path.insert(0, os.getcwd())
inputs = json.load(open(sys.argv[1], encoding="utf-8"))
out = {}
for t, blobs in inputs.items():
    mod, _, attr = t.partition(":")
    try:
        fn = getattr(importlib.import_module(mod), attr)
    except Exception as e:
        out[t] = {"error": "import: %s: %s" % (type(e).__name__, e)}
        continue
    res = []
    for b in blobs:
        try:
            a, k = pickle.loads(base64.b64decode(b))
        except Exception as e:
            res.append({"input": None, "error": "unpickle: %s" % e})
            continue
        shown = ", ".join([repr(x) for x in a] + ["%s=%r" % kv for kv in k.items()])[:200]
        try:
            r = "return " + repr(fn(*a, **k))
        except Exception as e:
            r = "raise %s: %s" % (type(e).__name__, e)
        res.append({"input": shown, "output": re.sub(r"0x[0-9a-fA-F]+", "0x?", r)[:500]})
    out[t] = res
json.dump(out, open(sys.argv[2], "w", encoding="utf-8"))
'''


# ---------------------------------------------------------------- purity

def _body_impure(node, aliases):
    for n in ast.walk(node):
        if isinstance(n, (ast.Global, ast.Nonlocal, ast.Yield, ast.YieldFrom, ast.Await)):
            return True
        if isinstance(n, ast.Name) and (n.id in IMPURE or aliases.get(n.id, "").split(".")[0] in IMPURE):
            return True
    return isinstance(node, ast.AsyncFunctionDef)


def pure_functions(graph, root):
    """Module-level functions whose body and internal callees avoid IO, clock, randomness and globals."""
    impure, trees = set(), {}
    for q, d in graph["defs"].items():
        tree = trees.get(d["file"])
        if tree is None:
            tree = trees[d["file"]] = ast.parse(Path(root, d["file"]).read_text(encoding="utf-8", errors="replace"))
        node = next((n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                     and n.lineno == d["line"]), None)
        if node is None or _body_impure(node, graph["aliases"].get(d["module"], {})):
            impure.add(q)
    changed = True
    while changed:  # propagate through internal references
        changed = False
        for q, refs in graph["calls"].items():
            if q not in impure and any(r in impure for r in refs):
                impure.add(q)
                changed = True
    return {q for q, d in graph["defs"].items()
            if q not in impure and d["kind"] == "function" and "." not in d["local"] and not d["test"]}


# ---------------------------------------------------------------- AI-generated tests (optional)

class GeneratedTest(BaseModel):
    code: str
    explanation: str


GEN_FORBIDDEN = {"open", "os", "subprocess", "socket", "requests", "httpx", "shutil", "pathlib", "eval", "exec",
                 "__import__", "importlib", "sys"}


def _validate_generated(code):
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"does not parse: {e}"
    if not any(isinstance(n, ast.FunctionDef) and n.name.startswith("test") for n in tree.body):
        return "defines no test function"
    bad = sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in GEN_FORBIDDEN} |
                 {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                  for a in (n.names if isinstance(n, ast.Import) else [ast.alias(n.module or "")])
                  if a.name.split(".")[0] in GEN_FORBIDDEN})
    return f"uses forbidden names {bad}" if bad else None


def _gen_args(py, gen_dir, target):
    return [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--rootdir={gen_dir}", str(target)]


def generated_tests(settings, provider, state, graph, funcs, py, runner, log=print, limit=3):
    """Ask the model for one test per pure function; keep it only if it passes on base; then run it on head."""
    gen_dir = settings.dir(f"generated_tests/{state['review_id']}").resolve()
    kept, discarded, head_failures = [], [], []
    for q in sorted(funcs)[:limit]:
        d = graph["defs"][q]
        src = "\n".join(Path(state["base_wt"], d["file"]).read_text(encoding="utf-8").splitlines()[d["line"] - 1:d["end_line"]])
        prompt = (f"Write a pytest test module for this function from module `{d['module']}` as it behaves NOW. Import it "
                  f"with `from {d['module']} import {d['local']}`. Use 2-4 concrete inputs and assert the exact outputs. "
                  f"No files, network, os or sys.\n\n```python\n{src[:3000]}\n```")
        name = re.sub(r"\W", "_", q)
        target = (gen_dir / f"test_gen_{name}.py").resolve()
        if gen_dir not in target.parents:  # the AI may only write inside generated_tests/
            raise PermissionError(f"generated test path escapes {gen_dir}")
        try:
            out, _ = provider.complete("test-writer", "You write small, deterministic pytest tests. JSON only.",
                                       prompt, GeneratedTest, 0.2, tag="review")
        except Exception as e:
            log_file = gen_dir / f"{name}.error.txt"
            log_file.write_text(f"{type(e).__name__}: {e}", encoding="utf-8")
            discarded.append({"test": target.name, "why": "model error", "ref": str(log_file)})
            continue
        target.write_text(out.code, encoding="utf-8")
        problem = _validate_generated(out.code)
        if problem:
            discarded.append({"test": target.name, "why": problem, "ref": str(target)})
            continue
        b = runner.run(_gen_args(py, gen_dir, target), state["base_wt"], f"gen-base-{name}")
        if not b["ok"]:
            discarded.append({"test": target.name, "why": "fails on base, so it does not describe base behaviour",
                              "ref": b["log"]})
            continue
        kept.append({"test": target.name, "function": q, "path": target})
    if kept:
        py_head, _ = review_env(settings, runner, state["head_wt"], log)  # head's own tree installed
        for k in kept:
            h = runner.run(_gen_args(py_head, gen_dir, k.pop("path")), state["head_wt"], f"gen-head-{k['test']}")
            k.update(head_ok=h["ok"], ref=h["log"])
            if not h["ok"]:
                head_failures.append({"test": k["test"], "function": k["function"], "ref": h["log"]})
    log(f"[review] generated tests: {len(kept)} kept, {len(discarded)} discarded, {len(head_failures)} fail on head")
    return {"requested": limit, "label": "VERIFIED (tests written by AI)", "dir": str(gen_dir), "kept": kept,
            "discarded": discarded, "head_failures": head_failures}


# ---------------------------------------------------------------- run

def acceptance_changes(base, head):
    out = []
    for e in head["matrix"]:
        b = next((x for x in base["matrix"] if (x["python"], x["mode"]) == (e["python"], e["mode"])), {})
        for n, t in e.get("acceptance_tests", {}).items():
            before = b.get("acceptance_tests", {}).get(n, {}).get("outcome")
            if before and before != t["outcome"]:
                out.append(f"py{e['python']}/{e['mode']} {n}: {before} -> {t['outcome']}")
    return out


def run(settings, provider, state, base_graph, head_graph, blast, base_result, head_result, out_dir,
        gen_tests=0, log=print):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    affected = {c["symbol"] for c in blast["changed"] if c["change"] == "modified"} | \
               {x["symbol"] for x in blast["direct_callers"] + blast["indirect_callers"]}
    pure = sorted(affected & pure_functions(base_graph, state["base_wt"]) & pure_functions(head_graph, state["head_wt"]))
    res = {"label": "VERIFIED", "pure_functions": pure, "inputs_replayed": 0, "diffs": [], "unstable": [],
           "acceptance_changes": acceptance_changes(base_result, head_result), "generated": {"requested": 0}}
    if not pure and not gen_tests:
        return res
    runner = CommandRunner([settings.repo, state["base_wt"], state["head_wt"], settings.work], out_dir / "logs",
                           min(settings.command_timeout, 300))
    py_base, _ = review_env(settings, runner, state["base_wt"], log)
    if py_base is None:
        res["error"] = "review venv unavailable"
        return res
    if pure:
        plugin_dir = out_dir / "plugin"
        plugin_dir.mkdir(exist_ok=True)
        (plugin_dir / "ra_capture_plugin.py").write_text(CAPTURE_PLUGIN, encoding="utf-8")
        (out_dir / "replay.py").write_text(REPLAY, encoding="utf-8")
        targets = [f"{base_graph['defs'][q]['module']}:{base_graph['defs'][q]['local']}" for q in pure]
        inputs = out_dir / "inputs.json"
        tests = sorted({t["file"] for t in blast["affected_tests"] if (Path(state["base_wt"]) / t["file"]).exists()})
        runner.run([py_base, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "ra_capture_plugin", *tests],
                   state["base_wt"], "capture-base", env={"PYTHONPATH": str(plugin_dir), "RA_CAPTURE_OUT": str(inputs),
                                                          "RA_CAPTURE_TARGETS": json.dumps(targets)})
        recorded = json.loads(inputs.read_text(encoding="utf-8")) if inputs.exists() else {}
        res["inputs_replayed"] = sum(len(v) for v in recorded.values())
        outs = {}
        for tag, wt in (("base1", state["base_wt"]), ("base2", state["base_wt"]), ("head", state["head_wt"])):
            py = py_base
            if tag == "head":
                py, _ = review_env(settings, runner, wt, log)  # reinstalls head's tree in package mode
            f = out_dir / f"replay-{tag}.json"
            r = runner.run([py, str(out_dir / "replay.py"), str(inputs), str(f)], wt, f"replay-{tag}")
            outs[tag] = (json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}, r["log"])
        for t in recorded:
            b1, b2, h = (outs[k][0].get(t) for k in ("base1", "base2", "head"))
            if not all(isinstance(x, list) for x in (b1, b2, h)):
                continue
            for x1, x2, y in zip(b1, b2, h):
                if "output" not in x1 or "output" not in y:
                    continue
                if x1["output"] != x2.get("output"):
                    res["unstable"].append({"function": t, "input": x1["input"]})
                elif x1["output"] != y["output"]:
                    res["diffs"].append({"function": t, "input": x1["input"], "base": x1["output"], "head": y["output"],
                                         "label": "VERIFIED", "ref": outs["head"][1]})
    if gen_tests and provider is not None:
        funcs = set(pure) or (affected & pure_functions(base_graph, state["base_wt"]))
        res["generated"] = generated_tests(settings, provider, state, base_graph, funcs, py_base, runner, log, gen_tests)
    elif gen_tests:
        res["generated"] = {"requested": gen_tests, "kept": [], "discarded": [], "head_failures": [],
                            "skipped": "AI disabled"}
    return res
