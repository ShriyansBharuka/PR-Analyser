#!/usr/bin/env python3
"""Find where each third-party dependency is imported and used in a repo.

Reads code with `ast` (Python's own parser; it reads code as a tree without running it).
Everything in the output is OBSERVED. Standard library only, Python 3.11+.

Usage:
    python find_usages.py --repo PATH [--out FILE]

Tip: run it with the target repo's virtualenv Python (where its deps are installed)
so import names like `dateutil` map to their package (`python-dateutil`) exactly.
"""
import argparse
import ast
import importlib.metadata
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from scan_deps import collect_declared, is_excluded, normalize, write_output

SKIP_DIRS = {"venv", "env", "build", "dist", "__pycache__", "node_modules", "site-packages"}
TEXT_FILE_RE = re.compile(r"^(Makefile|Dockerfile.*|Procfile|.*\.(cfg|ini|ya?ml|toml|sh|txt))$")


def iter_files(repo, exclude=()):
    """All files except virtualenvs, build output and hidden folders (but keep .github for CI files)."""
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs
                   if d not in SKIP_DIRS
                   and (not d.startswith(".") or d == ".github")
                   and not (Path(root, d) / "pyvenv.cfg").exists()
                   and not is_excluded(Path(root, d).relative_to(repo).as_posix(), exclude)]
        for f in files:
            if not is_excluded(Path(root, f).relative_to(repo).as_posix(), exclude):
                yield Path(root, f)


def module_name(path, repo):
    """repo/src/pkg/mod.py -> 'pkg.mod', repo/pkg/__init__.py -> 'pkg'."""
    rel = path.relative_to(repo)
    if rel.parts[0] == "src":
        rel = rel.relative_to("src")
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def resolve_from(mod, is_package, level, module):
    """Turn 'from ..x import y' inside 'pkg.sub.mod' into 'pkg.x'."""
    if level == 0:
        return module or ""
    base = mod.split(".") if is_package else mod.split(".")[:-1]
    base = base[: len(base) - (level - 1)] if level > 1 else base
    return ".".join(base + ([module] if module else []))


def dotted(node):
    """ast for `a.b.c` -> ['a', 'b', 'c'], or None if it is not a plain name chain."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return [node.id] + parts[::-1]
    return None


def is_private(symbol):
    return any(p.startswith("_") and not p.startswith("__") for p in symbol.split(".")[1:])


def analyze_file(path, repo):
    """Imports, resolved attribute uses and monkeypatches in one .py file."""
    source = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines()
    mod = module_name(path, repo)
    is_package = path.name == "__init__.py"
    imports, symbols, patches = [], [], []
    aliases = {}  # local name -> full dotted name it refers to

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imports.append({"line": node.lineno, "module": a.name, "names": []})
                if a.asname:
                    aliases[a.asname] = a.name
                else:
                    top = a.name.split(".")[0]
                    aliases[top] = top
        elif isinstance(node, ast.ImportFrom):
            base = resolve_from(mod, is_package, node.level, node.module)
            names = [a.name for a in node.names]
            imports.append({"line": node.lineno, "module": base, "names": names, "relative": node.level > 0})
            for a in node.names:
                aliases[a.asname or a.name] = f"{base}.{a.name}"
                if a.name != "*":
                    symbols.append((node.lineno, f"{base}.{a.name}"))

    def resolve(node):
        parts = dotted(node)
        if parts and parts[0] in aliases:
            return ".".join([aliases[parts[0]]] + parts[1:])
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            full = resolve(node)
            if full:
                symbols.append((node.lineno, full))
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Attribute):
                    full = resolve(t)
                    if full:
                        patches.append((node.lineno, full))

    code = lambda n: lines[n - 1].strip() if 0 < n <= len(lines) else ""
    for imp in imports:
        imp["code"] = code(imp["line"])
    return {"module": mod, "imports": imports, "symbols": symbols,
            "patches": [(n, t, code(n)) for n, t in patches]}


def find_usages(repo, exclude=()):
    repo = Path(repo).resolve()
    declared = collect_declared(repo, exclude)
    declared_keys = set(declared["packages"])
    # 'dateutil' -> 'python-dateutil', 'yaml' -> 'pyyaml'
    prefix_stripped = {re.sub(r"^(python-|py)", "", k): k for k in declared_keys}
    try:
        installed = importlib.metadata.packages_distributions()
    except Exception:  # broken metadata in the running env should not stop the scan
        installed = {}

    def map_import(top):
        candidates = [normalize(d) for d in installed.get(top, [])]
        if candidates:
            chosen = next((c for c in candidates if c in declared_keys), candidates[0])
            return chosen, "installed-metadata"
        n = normalize(top)
        if n in declared_keys:
            return n, "name-match"
        if n in prefix_stripped:
            return prefix_stripped[n], "prefix-match"
        return None, "unmapped"

    files, parse_errors = {}, []
    texts = []
    dep_files = set(declared["dep_files"])
    for path in iter_files(repo, exclude):
        rel = path.relative_to(repo).as_posix()
        if path.suffix == ".py":
            try:
                files[rel] = analyze_file(path, repo)
            except (SyntaxError, ValueError) as e:
                parse_errors.append(f"{rel}: {e}")
        elif TEXT_FILE_RE.match(path.name) and rel not in dep_files:
            texts.append((rel, path))

    local_modules = {f["module"] for f in files.values()}
    local_tops = {m.split(".")[0] for m in local_modules}
    stdlib = sys.stdlib_module_names

    packages = defaultdict(lambda: {"declared": False, "match_method": None, "import_names": set(),
                                    "usages": [], "api_symbols": set(), "private_api": [],
                                    "monkeypatches": [], "text_mentions": [], "covering_tests": set()})
    edges = defaultdict(set)      # local module -> local modules it imports
    direct = defaultdict(set)     # local module -> package keys it imports

    for rel, info in files.items():
        mod = info["module"]
        top_to_key = {}
        for imp in info["imports"]:
            target = imp["module"]
            top = target.split(".")[0]
            if imp.get("relative") or top in local_tops:
                for cand in [target] + [f"{target}.{n}" for n in imp["names"]]:
                    if cand in local_modules:
                        edges[mod].add(cand)
                continue
            if not top or top in stdlib:
                continue
            key, method = map_import(top)
            key = key or top
            pkg = packages[key]
            pkg["declared"] = key in declared_keys
            pkg["match_method"] = method
            pkg["import_names"].add(top)
            pkg["usages"].append({"file": rel, "line": imp["line"], "code": imp["code"]})
            top_to_key[top] = key
            direct[mod].add(key)
        for line, symbol in info["symbols"]:
            key = top_to_key.get(symbol.split(".")[0])
            if key:
                packages[key]["api_symbols"].add(symbol)
                if is_private(symbol):
                    packages[key]["private_api"].append({"file": rel, "line": line, "symbol": symbol})
        for line, target, code in info["patches"]:
            key = top_to_key.get(target.split(".")[0])
            if key:
                packages[key]["monkeypatches"].append({"file": rel, "line": line, "target": target, "code": code})

    # text mentions (Makefile, Dockerfile, CI ...) for every declared or imported package
    for key in declared_keys:
        packages[key]["declared"] = True
    patterns = {}
    for key, pkg in packages.items():
        words = {key, declared["packages"].get(key, {}).get("declared_as", key), *pkg["import_names"]}
        patterns[key] = re.compile("|".join(r"(?<![A-Za-z0-9_])" + re.escape(w) for w in words), re.I)
    for rel, path in texts:
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for key, pat in patterns.items():
                if pat.search(line):
                    packages[key]["text_mentions"].append({"file": rel, "line": n, "text": line.strip()})

    # tests whose import chain reaches each package (breadth-first walk over local imports)
    for rel, info in files.items():
        name = Path(rel).name
        if not (name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"):
            continue
        seen, queue = {info["module"]}, [info["module"]]
        while queue:
            m = queue.pop(0)
            for key in direct[m]:
                packages[key]["covering_tests"].add(rel)
            for nxt in edges[m] - seen:
                seen.add(nxt)
                queue.append(nxt)

    out = {}
    for key in sorted(packages):
        pkg = packages[key]
        # drop 'a.b' when 'a.b.c' is also listed, to keep the API list short
        syms = sorted(pkg["api_symbols"])
        pkg["api_symbols"] = [s for s in syms if not any(o.startswith(s + ".") for o in syms)]
        pkg["import_names"] = sorted(pkg["import_names"])
        pkg["covering_tests"] = sorted(pkg["covering_tests"])
        out[key] = pkg

    return {
        "tool": "find_usages",
        "label": "OBSERVED",
        "repo": str(repo),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scanner_python": sys.executable,
        "notes": [
            "match_method: installed-metadata = from packages installed in scanner_python; "
            "name-match / prefix-match = import name looks like a declared package name; "
            "unmapped = could not tell which package provides this import.",
            "covering_tests = test files whose import chain reaches the package (static import reach, not runtime coverage).",
            "Dynamic imports (importlib.import_module, __import__) are not detected.",
        ],
        "files_scanned": len(files),
        "packages": out,
        "undeclared_imports": [k for k, p in out.items() if not p["declared"] and p["match_method"] != "unmapped"],
        "unmapped_imports": [k for k, p in out.items() if p["match_method"] == "unmapped"],
        "declared_unused": [k for k, p in out.items() if p["declared"] and not p["usages"] and not p["text_mentions"]],
        "parse_errors": parse_errors,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="repo to scan (default: current folder)")
    ap.add_argument("--out", help="write JSON here instead of printing it")
    ap.add_argument("--exclude", nargs="*", default=[], help="repo-relative files/folders to ignore")
    args = ap.parse_args()
    if not Path(args.repo).is_dir():
        sys.exit(f"error: --repo {args.repo!r} is not a folder")
    write_output(find_usages(args.repo, args.exclude), args.out)


if __name__ == "__main__":
    main()
