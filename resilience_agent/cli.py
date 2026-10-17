"""Command-line interface: python -m resilience_agent <command>."""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import context, verification, workflow
from .config import Settings
from .llm import make_provider

console = Console()


def _settings(args):
    s = Settings()
    if getattr(args, "repo", None):
        s.repo = Path(args.repo).resolve()
    if getattr(args, "model", None):
        s.model = args.model
    if getattr(args, "offline", False):
        s.offline = True
    return s


def _provider(s):
    return make_provider(s, s.dir("artifacts/llm-calls"))


def log(msg):
    console.print(msg, highlight=False, markup=False)


# ---------------------------------------------------------------- commands

def cmd_doctor(args):
    s = _settings(args)
    rows = []
    rows.append(("Ollama", _provider(s).health()))
    dw = context.deepwiki_context(s, s.deepwiki_repo or context.guess_github_repo(context.repo_metadata(s.repo)["remote"]), retries=1)
    rows.append(("DeepWiki MCP", {"status": dw["status"], "detail": (dw.get("detail") or dw.get("structure") or "")[:160]}))
    rows.append(("Google Code Wiki context", context.codewiki_context(s) | {"text": None}))
    g = shutil.which("graphify")
    gv = subprocess.run([g, "--help"], capture_output=True, text=True) if g else None
    rows.append(("Graphify", {"status": "ready" if gv and gv.returncode == 0 else "unavailable", "detail": g or "not installed"}))
    d = shutil.which("docker")
    di = subprocess.run([d, "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True) if d else None
    rows.append(("Docker", {"status": "ready" if di and di.returncode == 0 else "unavailable",
                            "detail": (di.stdout or di.stderr).strip()[:120] if di else "not installed"}))
    gh = shutil.which("gh")
    ga = subprocess.run([gh, "auth", "status"], capture_output=True, text=True) if gh else None
    rows.append(("GitHub CLI", {"status": "authenticated" if ga and ga.returncode == 0 else "unauthenticated"}))
    for v in s.pythons:
        p = verification.find_python(v)
        rows.append((f"Python {v}", {"status": "ready" if p else "unavailable", "detail": p or ""}))
    rows.append(("uv", {"status": "ready" if shutil.which("uv") else "unavailable (pip fallback)"}))
    t = Table(title="resilience_agent doctor (real probes)")
    t.add_column("Service")
    t.add_column("Status")
    t.add_column("Detail", overflow="fold")
    for name, info in rows:
        st = info.get("status")
        color = "green" if st in ("ready", "available", "authenticated") else "yellow"
        t.add_row(name, f"[{color}]{st}[/{color}]", str(info.get("detail") or info.get("model") or ""))
    console.print(t)


def cmd_index(args):
    s = _settings(args)
    r = context.index(s, log=log)
    t = Table(title=f"Dependency inventory @ {r['meta']['commit'][:12]}")
    for col in ("package", "declared", "pinned", "latest", "used in", "risk flags"):
        t.add_column(col, overflow="fold")
    use = r["usages"]["packages"]
    for p in r["scan"]["packages"]:
        f = p.get("pypi") or {}
        t.add_row(p["name"], "; ".join(f"{x['file']}{x['spec'] and ' ' + x['spec']}" for x in p["sources"]),
                  p["pinned_version"] or "-", f.get("latest_version", "?"),
                  ", ".join(sorted({u["file"] for u in use.get(p["name"], {}).get("usages", [])})) or "-",
                  ", ".join(k for k, v in p["risk_flags"].items() if v))
    console.print(t)
    console.print(f"undeclared imports: {r['usages']['undeclared_imports']}  unmapped: {r['usages']['unmapped_imports']}"
                  f"  declared-unused: {r['usages']['declared_unused']}")
    console.print(f"artifacts: {r['artifact_dir']}")


def cmd_baseline(args):
    s = _settings(args)
    db = s.db()
    ix = db.get("index:latest") or context.index(s, db, log=log)
    b = verification.baseline(s, db, ix, log, refresh=args.refresh)
    _print_matrix(b, "Baseline")


def _print_matrix(result, title):
    t = Table(title=f"{title}: {result['label']}")
    keys = sorted({k for e in result["matrix"] for k in e.get("checks", {})})
    t.add_column("env")
    t.add_column("status")
    for k in keys:
        t.add_column(k)
    t.add_column("failing tests", overflow="fold")
    for e in result["matrix"]:
        fails = [n.split("::")[-1] for n, x in {**e.get("tests", {}), **e.get("acceptance_tests", {})}.items()
                 if x["outcome"] in ("failed", "error")]
        t.add_row(f"py{e['python']}/{e['mode']}", e["status"],
                  *[("ok" if e["checks"][k]["ok"] else "FAIL") if k in e.get("checks", {}) else "-" for k in keys],
                  ", ".join(fails) or e.get("reason", ""))
    console.print(t)


def cmd_scan(args):
    s = _settings(args)
    db = s.db()
    if args.reclassify:
        commit = db.get("discovery:latest")["commit"]
        workflow.save_issues(db, commit)
        console.print(f"re-applied evidence rules to the stored council votes for {commit[:12]} (no model calls)")
        return cmd_issues(args)
    g = workflow.discovery_graph(s, _provider(s), db, log)
    commit = context.repo_metadata(s.repo)["commit"]
    thread = f"scan-{commit[:12]}" + (f"-{args.fresh}" if args.fresh else "")
    workflow.drive(g, thread, {})
    cmd_issues(args)


def cmd_issues(args):
    db = _settings(args).db()
    t = Table(title="Maintenance issues (council + evidence rules)")
    for col in ("id", "classification", "severity", "reproduction", "votes D/S/E", "approval", "title"):
        t.add_column(col, overflow="fold")
    for i in sorted(db.issues(), key=lambda i: -i.get("score", 0)):
        v = i["discovery_votes"]
        votes = "/".join((v.get(k) or {}).get("verdict", "-").replace("real_issue", "real").replace("not_an_issue", "no")
                         for k in ("discovery", "skeptic", "evidence")) if v else "not reviewed"
        t.add_row(i["issue_id"], i["classification"], i["severity"], i["reproduction_status"], votes,
                  i["approval_status"], i["title"])
    console.print(t)


def cmd_inspect(args):
    db = _settings(args).db()
    i = db.issue(args.issue_id)
    if not i:
        sys.exit(f"unknown issue {args.issue_id}")
    console.rule(f"{i['issue_id']}: {i['title']}")
    console.print(f"classification: [bold]{i['classification']}[/bold] ({i['confidence_label']}), severity {i['severity']}, "
                  f"reproduction {i['reproduction_status']}, approval {i['approval_status']}, scope {i.get('scope')}")
    console.print(i["description"], highlight=False, markup=False)
    console.print("\n[bold]Evidence[/bold] (OBSERVED / EXECUTED)")
    for x in i["evidence"]:
        log(f"  {x['id']} [{x['kind']}] {x['text']}  <- {x['source']}")
    console.print("\n[bold]Council votes[/bold] (AI, independent)")
    for voter, x in i["discovery_votes"].items():
        if x:
            log(f"  {voter:9} {x['verdict']:12} {x['severity']:6} cites {x['cited_evidence']}: {x['rationale']}")
            for c in x.get("counterevidence", []):
                log(f"            counterevidence: {c}")
            for m in x.get("missing_evidence", []):
                log(f"            missing: {m}")
    a = i["adjudication"]
    console.print("\n[bold]Adjudication[/bold]")
    log(f"  proposed {a.get('proposed_classification')} -> final {i['classification']}: {a.get('rationale')}")
    for k in ("disagreements", "uncertainties", "rule_notes"):
        for x in a.get(k) or []:
            log(f"  {k[:-1]}: {x}")
    log(f"\n  target tests: {i.get('target_tests')}\n  approvals: {db.approvals(i['issue_id'])}")


def _decide(args, gate, decision):
    s = _settings(args)
    db = s.db()
    i = db.issue(args.issue_id)
    if not i:
        sys.exit(f"unknown issue {args.issue_id}")
    note = getattr(args, "note", "") or ""
    if gate == "issue":
        if getattr(args, "scope", None):
            i["scope"] = sorted(set(args.scope.split(",")))
            note += f" scope={i['scope']}"
        i["approval_status"] = {"approve": "approved", "reject": "rejected", "investigate": "needs_investigation"}[decision]
        db.save_issue(i)
    db.record_approval(args.issue_id, gate, decision, note.strip(), actor=getattr(args, "actor", "human-cli"))
    console.print(f"recorded {gate} decision '{decision}' for {args.issue_id} {note}")


def cmd_repair(args):
    s = _settings(args)
    db = s.db()
    if db.latest_decision(args.issue_id, "issue") != "approve":
        console.print(f"[yellow]{args.issue_id} is not approved. The run will pause at gate 1.[/yellow]")
    if getattr(args, "guidance", None):
        issue = db.issue(args.issue_id)
        issue.setdefault("maintainer_guidance", []).append({"text": args.guidance, "actor": "human-cli",
                                                           "at": __import__("resilience_agent.storage").storage.now()})
        db.save_issue(issue)
        console.print("maintainer guidance recorded (HUMAN) and added to every proposer prompt")
    n = db.get(f"repair-thread:{args.issue_id}", 0) + (1 if args.new else 0)
    db.put(f"repair-thread:{args.issue_id}", n)
    g = workflow.repair_graph(s, _provider(s), db, log)
    values, waiting = workflow.drive(g, f"repair-{args.issue_id}-{n}", {"issue_id": args.issue_id})
    console.print(f"status: [bold]{values.get('status')}[/bold]  waiting at: {waiting or 'nothing'}")
    if values.get("capsule"):
        console.print(f"capsule: {values['capsule']['dir']}")


def cmd_publish(args):
    _decide(args, "publication", "approve" if args.cmd == "publish" else "reject")
    args.new = False
    cmd_repair(args)


def cmd_verify(args):
    s = _settings(args)
    db = s.db()
    cands = [c for c in db.candidates(args.issue_id) if c.get("apply")]
    if not cands:
        sys.exit("no applied candidate to verify; run repair first")
    c = next((c for c in cands if c["cand_id"] == args.candidate), cands[-1]) if args.candidate else cands[-1]
    ix = db.get("index:latest")
    issue = db.issue(args.issue_id)
    from .repair import issue_gate
    r = verification.run_suite(s, c["apply"]["worktree"], f"{args.issue_id}-{c['cand_id']}-reverify",
                               verification.package_modules(ix), ix["entrypoints"], log)
    g = issue_gate(r, db.get(f"baseline:{issue['repository_commit']}"), issue)
    _print_matrix(r, "Re-verification")
    console.print(f"gate passed: {g['passed']} {g['reasons']}  fully green: {g['fully_green']}")


def cmd_report(args):
    s = _settings(args)
    p = s.work / "artifacts" / "capsules" / args.issue_id / "capsule.md"
    if not p.exists():
        sys.exit("no capsule yet; run repair first")
    console.print(p.read_text(encoding="utf-8"), markup=False, highlight=False)
    console.print(f"\n{p}\n{p.with_suffix('.json')}\n{p.parent / 'pr.md'}")


def cmd_query(args):
    s = _settings(args)
    c = context.Context(s.db().get("index:latest"))
    fn = {"where-used": c.where_used, "modules": c.modules_depending_on, "tests": c.tests_covering,
          "impact": c.impact, "migration-files": c.files_for_migration, "feature": c.find_feature}[args.what]
    console.print_json(json.dumps(fn(args.arg)))


def cmd_review(args):
    """Review mode: compare base and head of an incoming change. Maintenance commands are unaffected."""
    s = _settings(args)
    db = s.db()
    desc = Path(args.pr_description).read_text(encoding="utf-8") if args.pr_description else ""
    from .review import resolve_ref
    b, h = resolve_ref(s.repo, args.base), resolve_ref(s.repo, args.head)
    review_id = f"review-{b[:8]}-{h[:8]}"
    g = workflow.review_graph(s, _provider(s), db, log)
    values, _ = workflow.drive(g, review_id + (f"-{args.fresh}" if args.fresh else ""), {
        "review_id": review_id, "base": args.base, "head": args.head, "pr_description": desc,
        "full_suite": args.full_suite, "gen_tests": args.gen_tests, "use_ai": not args.no_ai})
    v = db.get(f"review:{review_id}:verdict") or {}
    console.print(f"\nverdict: [bold]{v.get('verdict')}[/bold]")
    for r in v.get("reasons", []):
        log(f"  [{r['label']}] {r['text']}")
    if values.get("capsule"):
        console.print(f"capsule: {values['capsule']['dir']}")


def cmd_run(args):
    """Guided pipeline; stops only at the human gates and asks the person at the terminal."""
    cmd_doctor(args)
    args.fresh, args.reclassify = None, False
    cmd_scan(args)
    s = _settings(args)
    db = s.db()
    issue_id = input("\nIssue id to repair (blank = stop): ").strip()
    if not issue_id:
        return
    cmd_inspect(argparse.Namespace(**vars(args) | {"issue_id": issue_id}))
    answer = input(f"\nApprove {issue_id} for repair? [y/N/scope=a,b]: ").strip()
    if not answer.lower().startswith(("y", "scope=")):
        console.print("not approved; stopping (state is saved)")
        return
    scope = answer.split("=", 1)[1] if answer.startswith("scope=") else None
    _decide(argparse.Namespace(**vars(args) | {"issue_id": issue_id, "scope": scope, "note": "interactive run"}), "issue", "approve")
    cmd_repair(argparse.Namespace(**vars(args) | {"issue_id": issue_id, "new": True}))
    if input("\nApprove publication (prepare PR branch)? [y/N]: ").strip().lower().startswith("y"):
        cmd_publish(argparse.Namespace(**vars(args) | {"issue_id": issue_id, "cmd": "publish", "note": "interactive"}))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m resilience_agent", description="Evidence-driven maintenance agent")
    ap.add_argument("--repo", help="target repository (default: current folder)")
    ap.add_argument("--model", help="Ollama model, e.g. qwen2.5-coder:7b")
    ap.add_argument("--offline", action="store_true", help="skip PyPI and DeepWiki")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sub.add_parser("index").set_defaults(fn=cmd_index)
    p = sub.add_parser("baseline")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(fn=cmd_baseline)
    p = sub.add_parser("scan")
    p.add_argument("--fresh", help="start a new scan thread with this tag")
    p.add_argument("--reclassify", action="store_true", help="re-apply evidence rules to stored votes")
    p.set_defaults(fn=cmd_scan)
    sub.add_parser("issues").set_defaults(fn=cmd_issues)
    for name, gate, decision in (("approve", "issue", "approve"), ("reject", "issue", "reject"),
                                 ("investigate", "issue", "investigate")):
        p = sub.add_parser(name)
        p.add_argument("issue_id")
        p.add_argument("--note", default="")
        if name == "approve":
            p.add_argument("--scope", help="comma-separated dependencies to include in the repair scope")
        p.set_defaults(fn=lambda a, g=gate, d=decision: _decide(a, g, d))
    for name in ("inspect", "report"):
        p = sub.add_parser(name)
        p.add_argument("issue_id")
        p.set_defaults(fn={"inspect": cmd_inspect, "report": cmd_report}[name])
    p = sub.add_parser("repair")
    p.add_argument("issue_id")
    p.add_argument("--new", action="store_true", help="start a fresh repair thread")
    p.add_argument("--guidance", help="maintainer guidance for the proposers (recorded as HUMAN input)")
    p.set_defaults(fn=cmd_repair)
    p = sub.add_parser("verify")
    p.add_argument("issue_id")
    p.add_argument("--candidate")
    p.set_defaults(fn=cmd_verify)
    for name in ("publish", "reject-publication"):
        p = sub.add_parser(name)
        p.add_argument("issue_id")
        p.add_argument("--note", default="")
        p.set_defaults(fn=cmd_publish)
    p = sub.add_parser("query")
    p.add_argument("what", choices=["where-used", "modules", "tests", "impact", "migration-files", "feature"])
    p.add_argument("arg")
    p.set_defaults(fn=cmd_query)
    sub.add_parser("run").set_defaults(fn=cmd_run)
    p = sub.add_parser("review", help="review mode: compare a base and a head commit")
    p.add_argument("--base", default="main")
    p.add_argument("--head", required=True, help="branch, tag or commit to review")
    p.add_argument("--pr-description", help="file with the PR description (enables the AI intent review)")
    p.add_argument("--full-suite", action="store_true", help="run every test instead of only affected ones")
    p.add_argument("--gen-tests", type=int, default=0, help="AI-generated tests for up to N pure functions (0 = off)")
    p.add_argument("--no-ai", action="store_true", help="skip every model call")
    p.add_argument("--fresh", help="start a new review thread with this tag")
    p.set_defaults(fn=cmd_review)
    args = ap.parse_args(argv)
    args.fn(args)
