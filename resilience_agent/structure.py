"""Deterministic structural checks between base and head. No model is involved: everything is OBSERVED.

- public API diff (griffe: removed objects, moved/removed parameters, changed defaults ...) + likely renames
- import cycles introduced by head
- optional layer rules from pr-analyser.toml (read from BASE, so a PR cannot relax its own rules)
- packaging: entry points, declared dependencies, new undeclared imports (existing scanners)
"""
import difflib
import json
import tomllib
from pathlib import Path

from .context import _find_usages, _scan_deps

LAYER_FILE = "pr-analyser.toml"


# ---------------------------------------------------------------- public API

def _packages(facts):
    return sorted({m.split(".")[0] for m in facts["package_modules"]})


def _pkg_path(tree, pkg):
    for p in (Path(tree) / "src" / pkg, Path(tree) / pkg, Path(tree) / "src" / f"{pkg}.py", Path(tree) / f"{pkg}.py"):
        if p.exists():
            return p
    raise FileNotFoundError(f"{pkg} not found in {tree}")


def api_diff(base_wt, head_wt, packages):
    """griffe breakages per top-level package. Code is parsed statically (allow_inspection=False), never imported.

    Loaded by file path, not by name: a name lookup also searches sys.path and can silently pick up another copy."""
    import griffe
    out, errors = [], []
    for pkg in packages:
        try:
            old = griffe.load(_pkg_path(base_wt, pkg), allow_inspection=False)
        except Exception as e:  # package added in head, or unparseable: nothing to break
            errors.append(f"{pkg} (base): {type(e).__name__}: {e}")
            continue
        try:
            new = griffe.load(_pkg_path(head_wt, pkg), allow_inspection=False)
        except Exception as e:
            out.append({"kind": "Public object was removed", "symbol": pkg, "old": pkg, "new": None,
                        "explanation": f"package {pkg} can no longer be loaded: {e}", "label": "OBSERVED"})
            continue
        for b in griffe.find_breaking_changes(old, new):
            d = b.as_dict()
            kind = d["kind"].value
            old_v, new_v = d.get("old_value"), d.get("new_value")
            detail = {"Parameter default was changed": lambda: f" ({old_v.name}: {old_v.default} -> {new_v.default})",
                      "Parameter is now required": lambda: f" ({old_v.name})",
                      "Parameter was removed": lambda: f" ({old_v.name})",
                      "Parameter was added as required": lambda: f" ({new_v.name})",
                      "Positional parameter was moved": lambda: f" ({old_v.name})"}.get(kind, lambda: "")()
            out.append({"kind": kind, "symbol": d["object_path"], "old": _short(old_v), "new": _short(new_v),
                        "explanation": f"{d['object_path']}: {kind}{detail}", "label": "OBSERVED"})
    return out, errors


def _short(v):
    return None if v is None else str(getattr(v, "name", v))[:200]


def likely_renames(breaking, base_graph, head_graph):
    """A removed public def paired with a new public def of the same kind, module, signature and near-identical body."""
    removed = {b["symbol"] for b in breaking if b["kind"] == "Public object was removed"}
    added = [q for q in head_graph["defs"] if q not in base_graph["defs"] and head_graph["defs"][q]["public"]]
    out = {}
    for q in removed:
        old = base_graph["defs"].get(q)
        if not old:
            continue
        best = None
        for n in added:
            new = head_graph["defs"][n]
            if (new["kind"], new["module"], new["args"]) != (old["kind"], old["module"], old["args"]):
                continue
            ratio = difflib.SequenceMatcher(None, _body(base_graph, q), _body(head_graph, n)).ratio()
            if ratio >= 0.8 and (not best or ratio > best[1]):
                best = (n, ratio)
        if best:
            out[q] = {"new_name": best[0], "similarity": round(best[1], 2)}
    return out


def _body(graph, q):
    d = graph["defs"][q]
    lines = graph.setdefault("_src", {}).setdefault(d["file"], None)
    if lines is None:
        lines = graph["_src"][d["file"]] = Path(graph["root"], d["file"]).read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[d["line"]:d["end_line"]])  # without the def line, so the name does not count


# ---------------------------------------------------------------- import cycles and layers

def cycles(imports):
    """Strongly connected components with more than one module (Tarjan, iterative)."""
    index, low, stack, on, out, counter = {}, {}, [], set(), [], [0]
    for root in sorted(imports):
        if root in index:
            continue
        work = [(root, iter(sorted(imports.get(root, ()))))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            nxt = next(it, None)
            if nxt is not None:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on.add(nxt)
                    work.append((nxt, iter(sorted(imports.get(nxt, ())))))
                elif nxt in on:
                    low[v] = min(low[v], index[nxt])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    out.append(sorted(comp))
    return sorted(out)


def _matches(mod, prefix):
    return mod == prefix or mod.startswith(prefix + ".")


def layer_violations(imports, rules):
    """Each rule {source, target}: no module under `source` may reach a module under `target`, directly or not."""
    found = []
    for rule in rules:
        for start in sorted(m for m in imports if _matches(m, rule["source"])):
            prev, frontier = {start: None}, [start]
            while frontier:
                nxt = []
                for m in frontier:
                    for t in sorted(imports.get(m, ())):
                        if t not in prev:
                            prev[t] = m
                            nxt.append(t)
                frontier = nxt
            for hit in sorted(m for m in prev if m != start and _matches(m, rule["target"]) and not _matches(m, rule["source"])):
                chain, cur = [], hit
                while cur:
                    chain.append(cur)
                    cur = prev[cur]
                found.append({"rule": f"{rule['source']} must not import {rule['target']}", "chain": chain[::-1]})
    return found


def load_rules(tree):
    path = Path(tree) / LAYER_FILE
    if not path.exists():
        return [], None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        return [], f"{LAYER_FILE}: {e}"
    return [r for r in data.get("forbid", []) if r.get("source") and r.get("target")], None


# ---------------------------------------------------------------- packaging

def packaging(base_wt, head_wt, facts, exclude):
    def declared(tree):
        return {n: sorted(f"{s['file']}:{s['spec'] or '*'}" for s in p["sources"] if s["group"] != "build-system")
                for n, p in _scan_deps.collect_declared(tree, exclude)["packages"].items()}
    bd, hd = declared(base_wt), declared(head_wt)
    be, he = facts["base"]["entrypoints"], facts["head"]["entrypoints"]
    bu = set(_find_usages.find_usages(base_wt, exclude)["undeclared_imports"])
    hu = set(_find_usages.find_usages(head_wt, exclude)["undeclared_imports"])
    return {"label": "OBSERVED",
            "entry_points": {"added": sorted(f"{k} = {he[k]}" for k in set(he) - set(be)),
                             "removed": sorted(f"{k} = {be[k]}" for k in set(be) - set(he)),
                             "changed": sorted(f"{k}: {be[k]} -> {he[k]}" for k in set(be) & set(he) if be[k] != he[k])},
            "dependencies": {"added": sorted(set(hd) - set(bd)), "removed": sorted(set(bd) - set(hd)),
                             "changed": sorted(f"{k}: {bd[k]} -> {hd[k]}" for k in set(bd) & set(hd) if bd[k] != hd[k])},
            "new_undeclared_imports": sorted(hu - bu)}


# ---------------------------------------------------------------- all checks

def check(settings, base_wt, head_wt, base_graph, head_graph, facts, diff, out_path):
    base_graph["root"], head_graph["root"] = str(base_wt), str(head_wt)
    pkgs = sorted(set(_packages(facts["base"])) | set(_packages(facts["head"])))
    breaking, errors = api_diff(base_wt, head_wt, pkgs)
    renames = likely_renames(breaking, base_graph, head_graph)
    for b in breaking:
        if b["symbol"] in renames:
            r = renames[b["symbol"]]
            b["likely_rename_to"] = r["new_name"]
            b["explanation"] += f" - likely renamed to {r['new_name']} (body {int(r['similarity'] * 100)}% identical)"
    bc, hc = cycles(base_graph["imports"]), cycles(head_graph["imports"])
    rules, rule_error = load_rules(base_wt)
    bl, hl = layer_violations(base_graph["imports"], rules), layer_violations(head_graph["imports"], rules)
    res = {"label": "OBSERVED", "ref": str(out_path),
           "api": {"packages": pkgs, "breaking": breaking, "errors": errors},
           "cycles": {"base": bc, "head": hc, "new": [c for c in hc if c not in bc]},
           "layers": {"rules_file": LAYER_FILE if rules else None, "rules": rules, "error": rule_error,
                      "new": [v for v in hl if v not in bl], "pre_existing": [v for v in hl if v in bl]},
           "packaging": packaging(base_wt, head_wt, facts, settings.exclude),
           "config_files_changed": sorted(p for p in diff if p.endswith((".toml", ".cfg", ".ini", "setup.py")))}
    Path(out_path).write_text(json.dumps(res, indent=2), encoding="utf-8")
    return res
