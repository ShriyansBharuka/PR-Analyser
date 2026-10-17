"""Level 1: context engine and repository knowledge base.

Source priority (lower = more trusted): 1 checked-out code/tests, 2 dependency metadata and tool
output, 3 git history/docs, 4 Graphify relationships, 5 DeepWiki/Code Wiki text, 6 LLM hypotheses.
"""
import ast
import asyncio
import json
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from .config import SKILL_SCRIPTS, Settings
from .storage import now

sys.path.insert(0, str(SKILL_SCRIPTS))
import find_usages as _find_usages  # noqa: E402  (stdlib-only scanners shared with the Agent Skill)
import scan_deps as _scan_deps  # noqa: E402

PRIORITY = {"repository_code": 1, "dependency_metadata": 2, "tool_output": 2, "executed_check": 1,
            "git_history": 3, "documentation": 3, "graphify": 4, "deepwiki": 5, "codewiki": 5, "llm": 6}


def git(repo, *args):
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)
    return out.stdout.strip() if out.returncode == 0 else None


def repo_metadata(repo):
    status = git(repo, "status", "--porcelain") or ""
    return {
        "path": str(repo),
        "commit": git(repo, "rev-parse", "HEAD"),
        "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "remote": git(repo, "remote", "get-url", "origin"),
        "dirty_files": [l[3:] for l in status.splitlines()],
        "recent_commits": (git(repo, "log", "-10", "--format=%h %ad %s", "--date=short") or "").splitlines(),
    }


def module_inventory(repo, exclude):
    """Classes, functions and imports per Python module, with line numbers (EXTRACTED from AST)."""
    modules = []
    for path in _find_usages.iter_files(repo, exclude):
        if path.suffix != ".py":
            continue
        rel = path.relative_to(repo).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as e:
            modules.append({"file": rel, "error": str(e)})
            continue
        symbols = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols.append({"name": node.name, "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                                "line": node.lineno, "end_line": node.end_lineno,
                                "decorators": [ast.unparse(d) for d in getattr(node, "decorator_list", [])],
                                "doc": (ast.get_docstring(node) or "").split("\n")[0]})
        modules.append({"file": rel, "module": _find_usages.module_name(path, repo),
                        "is_test": Path(rel).name.startswith("test_") or Path(rel).name == "conftest.py",
                        "lines": len(path.read_text(encoding="utf-8", errors="replace").splitlines()),
                        "symbols": symbols})
    return modules


def entrypoints(repo):
    py = repo / "pyproject.toml"
    if not py.exists():
        return {}
    try:
        return tomllib.loads(py.read_text(encoding="utf-8")).get("project", {}).get("scripts", {})
    except tomllib.TOMLDecodeError:
        return {}


# ---------------------------------------------------------------- external context (untrusted text)

def _cache_get(settings, name, max_age=24 * 3600):
    path = settings.dir("cache") / name
    if path.exists() and time.time() - path.stat().st_mtime < max_age:
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _cache_put(settings, name, data):
    (settings.dir("cache") / name).write_text(json.dumps(data, indent=2), encoding="utf-8")


def guess_github_repo(remote):
    if not remote or "github.com" not in remote:
        return None
    tail = remote.split("github.com")[-1].lstrip(":/").removesuffix(".git")
    return tail if tail.count("/") == 1 else None


async def _deepwiki_call(repo_name, timeout):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    async with streamablehttp_client("https://mcp.deepwiki.com/mcp", timeout=timeout) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = [t.name for t in (await session.list_tools()).tools]
            result = await session.call_tool("read_wiki_structure", {"repoName": repo_name})
            text = "\n".join(getattr(c, "text", "") for c in result.content)
            contents = ""
            if not result.isError and "read_wiki_contents" in tools:
                full = await session.call_tool("read_wiki_contents", {"repoName": repo_name})
                contents = "\n".join(getattr(c, "text", "") for c in full.content)
            return {"tools": tools, "structure": text, "contents": contents, "is_error": bool(result.isError)}


def deepwiki_context(settings, repo_name, retries=3):
    """DeepWiki through its real MCP server, with cache, timeout and exponential backoff."""
    if not repo_name:
        return {"status": "unavailable", "detail": "no GitHub repo name"}
    cache_name = f"deepwiki-{repo_name.replace('/', '__')}.json"
    cached = _cache_get(settings, cache_name)
    if cached:
        return cached | {"cached": True}
    if settings.offline:
        return {"status": "unavailable", "detail": "offline mode"}
    error = None
    for attempt in range(retries):
        try:
            data = asyncio.run(asyncio.wait_for(_deepwiki_call(repo_name, 30), 90))
            text = (data["structure"] + data["contents"]).lower()
            not_indexed = data["is_error"] or "not found" in text[:500] or "no wiki" in text[:500]
            out = {"status": "not_indexed" if not_indexed else "ready", "repo": repo_name,
                   "fetched_at": now(), "source": "https://mcp.deepwiki.com/mcp", **data}
            if out["status"] == "ready":  # never cache failures or "not indexed"
                _cache_put(settings, cache_name, out)
            return out
        except Exception as e:  # network, protocol and timeout errors all degrade gracefully
            error = f"{type(e).__name__}: {e}"
            time.sleep(2 ** attempt)
    return {"status": "unavailable", "detail": error, "repo": repo_name}


def codewiki_context(settings):
    """Google Code Wiki has no documented public API, so we import saved pages from .resilience/codewiki/."""
    folder = settings.dir("codewiki")
    files = sorted(p for p in folder.glob("*") if p.suffix in (".md", ".txt", ".html"))
    if not files:
        return {"status": "not_imported", "detail": f"save Code Wiki pages for this repo into {folder}"}
    return {"status": "available", "files": [str(p) for p in files],
            "text": "\n\n".join(p.read_text(encoding="utf-8", errors="replace") for p in files)[:20000]}


def graphify_graph(settings, repo, out_dir: Path):
    """Run Graphify's supported CLI (`graphify update`, no LLM needed) and copy its outputs."""
    exe = shutil.which("graphify") or str(Path(sys.executable).parent / "graphify")
    if not shutil.which(exe) and not Path(exe + ".exe").exists():
        return {"status": "unavailable", "detail": "graphify CLI not installed (pip install graphifyy)"}
    try:
        run = subprocess.run([exe, "update", str(repo)], capture_output=True, text=True, timeout=300,
                             encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"status": "unavailable", "detail": f"{type(e).__name__}: {e}"}
    src = repo / "graphify-out"
    if run.returncode != 0 or not (src / "graph.json").exists():
        return {"status": "unavailable", "detail": (run.stderr or run.stdout)[-1500:]}
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("graph.json", "graph.html", "GRAPH_REPORT.md"):
        if (src / name).exists():
            shutil.copy2(src / name, out_dir / name)
    graph = json.loads((src / "graph.json").read_text(encoding="utf-8"))
    return {"status": "ready", "nodes": len(graph.get("nodes", [])), "links": len(graph.get("links", [])),
            "built_at_commit": graph.get("built_at_commit"), "out_dir": str(out_dir),
            "log": run.stdout[-800:]}


def dependency_view(usages):
    """Lightweight dependency graph for the maintenance UI: package -> files -> tests."""
    nodes, edges = [], []
    for pkg, info in usages["packages"].items():
        nodes.append({"id": f"pkg:{pkg}", "type": "package", "declared": info["declared"]})
        for u in info["usages"]:
            nodes.append({"id": f"file:{u['file']}", "type": "file"})
            edges.append({"from": f"file:{u['file']}", "to": f"pkg:{pkg}", "line": u["line"]})
        for t in info["covering_tests"]:
            edges.append({"from": f"test:{t}", "to": f"pkg:{pkg}", "kind": "import-reach"})
            nodes.append({"id": f"test:{t}", "type": "test"})
    unique = {n["id"]: n for n in nodes}
    return {"nodes": list(unique.values()), "edges": edges}


# ---------------------------------------------------------------- facts

def build_facts(meta, modules, scan, usages, external):
    commit = meta["commit"]

    def fact(kind, subject, claim, source_type, source=None, line=None, status="extracted", data=None):
        return {"kind": kind, "subject": subject, "claim": claim, "source_type": source_type, "source": source,
                "line": line, "status": status, "priority": PRIORITY[source_type], "data": data or {},
                "commit": commit}

    facts = [fact("repo", meta["path"], f"HEAD is {commit} on branch {meta['branch']}", "git_history")]
    for m in modules:
        for s in m.get("symbols", []):
            facts.append(fact("symbol", m["file"], f"{s['kind']} {s['name']} defined", "repository_code",
                              m["file"], s["line"], data=s))
    for p in scan["packages"]:
        for src in p["sources"]:
            facts.append(fact("declaration", p["name"], f"declared as '{p['declared_as']}{src['spec']}' ({src['group']})",
                              "repository_code", src["file"], src["line"], data=src))
        if p.get("pypi") and "error" not in p["pypi"]:
            facts.append(fact("pypi", p["name"], f"latest {p['pypi']['latest_version']}, last release "
                              f"{(p['pypi']['last_release_date'] or '')[:10]}", "dependency_metadata",
                              f"https://pypi.org/project/{p['name']}/", data=p["pypi"]))
        flags = [k for k, v in p["risk_flags"].items() if v]
        if flags:
            facts.append(fact("risk_flags", p["name"], ", ".join(flags), "tool_output", "scan_deps.py", data=p["risk_flags"]))
    for pkg, info in usages["packages"].items():
        for u in info["usages"]:
            facts.append(fact("usage", pkg, u["code"], "repository_code", u["file"], u["line"]))
        for sym in info["api_symbols"]:
            facts.append(fact("api_symbol", pkg, sym, "repository_code", data={"symbol": sym}))
        for x in info["private_api"]:
            facts.append(fact("private_api", pkg, x["symbol"], "repository_code", x["file"], x["line"]))
        for x in info["monkeypatches"]:
            facts.append(fact("monkeypatch", pkg, x["code"], "repository_code", x["file"], x["line"]))
        for t in info["covering_tests"]:
            facts.append(fact("test_reach", pkg, f"{t} imports code that uses {pkg}", "tool_output", t,
                              status="extracted"))
    for name in ("deepwiki", "codewiki"):
        ext = external.get(name, {})
        if ext.get("status") in ("ready", "available"):
            text = ext.get("contents") or ext.get("structure") or ext.get("text") or ""
            facts.append(fact("external_summary", meta["path"], text[:4000], name,
                              ext.get("source") or ",".join(ext.get("files", [])), status="inferred"))
    graph = external.get("graphify", {})
    if graph.get("status") == "ready":
        facts.append(fact("graph", meta["path"], f"Graphify graph: {graph['nodes']} nodes, {graph['links']} links",
                          "graphify", graph["out_dir"], data=graph))
    return facts


def index(settings: Settings, db=None, with_external=True, log=print):
    """Inspect the repository and persist the knowledge base. Returns a summary dict."""
    db = db or settings.db()
    repo = settings.repo
    meta = repo_metadata(repo)
    out = settings.dir(f"artifacts/index/{meta['commit'][:12]}")
    log(f"[index] repo {repo} @ {meta['commit'][:12]} ({meta['branch']})")
    modules = module_inventory(repo, settings.exclude)
    scan = _scan_deps.scan(repo, offline=settings.offline, exclude=settings.exclude)
    usages = _find_usages.find_usages(repo, settings.exclude)
    log(f"[index] {len(modules)} modules, {len(scan['packages'])} declared deps, "
        f"{len(usages['packages'])} imported/declared packages")
    external = {"deepwiki": {"status": "skipped"}, "codewiki": {"status": "skipped"}, "graphify": {"status": "skipped"}}
    if with_external:
        dw_repo = settings.deepwiki_repo or guess_github_repo(meta["remote"])
        external["deepwiki"] = deepwiki_context(settings, dw_repo)
        log(f"[index] DeepWiki ({dw_repo}): {external['deepwiki']['status']}")
        external["codewiki"] = codewiki_context(settings)
        log(f"[index] Code Wiki: {external['codewiki']['status']}")
        external["graphify"] = graphify_graph(settings, repo, out / "graphify")
        log(f"[index] Graphify: {external['graphify']['status']}")
    facts = build_facts(meta, modules, scan, usages, external)
    db.replace_facts(meta["commit"], facts)
    result = {"meta": meta, "collected_at": now(), "modules": modules, "scan": scan, "usages": usages,
              "entrypoints": entrypoints(repo), "dependency_view": dependency_view(usages),
              "external": {k: {kk: vv for kk, vv in v.items() if kk not in ("contents", "text")}
                           for k, v in external.items()},
              "fact_count": len(facts), "artifact_dir": str(out)}
    for name, data in (("index.json", result), ("dependency_inventory.json", scan), ("usages.json", usages),
                       ("modules.json", modules), ("external_context.json", external),
                       ("dependency_view.json", result["dependency_view"])):
        (out / name).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    db.put("index:latest", result)
    log(f"[index] {len(facts)} facts stored; artifacts in {out}")
    return result


# ---------------------------------------------------------------- queries over the knowledge base

class Context:
    def __init__(self, index_result):
        self.ix = index_result
        self.usages = index_result["usages"]["packages"]
        self.modules = {m["file"]: m for m in index_result["modules"] if "module" in m}

    def where_used(self, dep):
        return self.usages.get(dep, {}).get("usages", [])

    def modules_depending_on(self, dep):
        return sorted({u["file"] for u in self.where_used(dep)})

    def tests_covering(self, dep):
        return self.usages.get(dep, {}).get("covering_tests", [])

    def impact(self, dep):
        """What could break if `dep` changes: files using it, their importers, and reaching tests."""
        files = self.modules_depending_on(dep)
        mods = {self.modules[f]["module"] for f in files if f in self.modules}
        importers = set()
        for f, m in self.modules.items():
            src = Path(self.ix["meta"]["path"], f).read_text(encoding="utf-8", errors="replace")
            if any(mod.split(".")[-1] in src for mod in mods) and f not in files:
                importers.add(f)
        return {"files": files, "importers": sorted(importers), "tests": self.tests_covering(dep),
                "api_symbols": self.usages.get(dep, {}).get("api_symbols", [])}

    def files_for_migration(self, dep):
        manifests = sorted({s["file"] for p in self.ix["scan"]["packages"] if p["name"] == dep for s in p["sources"]})
        return manifests + self.modules_depending_on(dep)

    def find_feature(self, word):
        word = word.lower()
        return [{"file": f, **s} for f, m in self.modules.items() for s in m["symbols"]
                if word in s["name"].lower() or word in s["doc"].lower()]
