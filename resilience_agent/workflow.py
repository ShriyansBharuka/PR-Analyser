"""LangGraph state machines with SQLite checkpoints.

discovery graph: inspect_repository -> collect_repository_context -> run_baseline -> generate_issue_candidates
                 -> discovery_council -> adjudicate_issues
repair graph:    await_issue_approval* -> generate_repair_candidates -> cross_review_candidates -> revise_candidates
                 -> rank_repair_candidates -> apply_selected_patch -> run_verification -> evaluate_verification
                 -> (revise_or_retry -> rank_repair_candidates ...)* -> generate_exit_capsule
                 -> await_publication_approval* -> prepare_pull_request -> refresh_knowledge_graph
(* = human gate implemented with langgraph interrupt(); the run pauses, persists, and resumes later)
review graph:    prepare_review -> map_blast_radius -> structural_checks -> verify_base -> verify_head
                 -> behaviour_diff -> intent_review -> decide_verdict -> generate_review_capsule
"""
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import context, discovery, history, repair, reports, verification
from .storage import now


class DiscoveryState(TypedDict, total=False):
    repo: dict
    commit: str
    status: str
    candidates: list
    issues: list
    errors: list


class RepairState(TypedDict, total=False):
    issue_id: str
    commit: str
    status: str
    attempt: int
    candidate_ids: list
    selected: str
    branch: str
    patch: str
    weights: dict
    weights_history: list
    ranking_history: list
    feedback: str
    verification_labels: list
    capsule: dict
    publication: str
    pr: dict
    graph_refresh: dict
    errors: list


def _checkpointer(settings):
    return SqliteSaver(sqlite3.connect(settings.work / "checkpoints.sqlite", check_same_thread=False))


# ---------------------------------------------------------------- discovery graph

def discovery_graph(settings, provider, db, log=print):
    def inspect_repository(state):
        meta = context.repo_metadata(settings.repo)
        log(f"[inspect] {meta['path']} @ {meta['commit'][:12]} ({meta['branch']}), "
            f"{len(meta['dirty_files'])} uncommitted file(s)")
        return {"repo": meta, "commit": meta["commit"], "status": "inspected", "errors": []}

    def collect_repository_context(state):
        ix = context.index(settings, db, log=log)
        return {"status": "indexed", "commit": ix["meta"]["commit"]}

    def run_baseline(state):
        ix = db.get("index:latest")
        base = verification.baseline(settings, db, ix, log)
        log(f"[baseline] gate passed: {base['gate']['passed']}; failures: {len(base['gate']['failures'])}")
        return {"status": "baselined"}

    def generate_issue_candidates(state):
        cands = discovery.build_candidates(db.get("index:latest"), db.get(f"baseline:{state['commit']}"))
        db.put(f"candidates:{state['commit']}", cands)
        log(f"[candidates] {len(cands)}: " + ", ".join(f"{c['candidate_id']}({c['score']})" for c in cands))
        return {"candidates": [c["candidate_id"] for c in cands], "status": "candidates"}

    def discovery_council(state):
        cands = db.get(f"candidates:{state['commit']}")
        reviewed = cands[: settings.council_limit]
        votes, adj = discovery.run_council(provider, reviewed, db, state["commit"], log) if reviewed else ({}, {"decisions": []})
        db.put(f"council:{state['commit']}", {"votes": votes, "adjudication": adj, "reviewed": [c["candidate_id"] for c in reviewed]})
        return {"status": "voted"}

    def adjudicate_issues(state):
        ids = save_issues(db, state["commit"], record_votes=True)
        return {"issues": ids, "status": "awaiting_issue_approval"}

    g = StateGraph(DiscoveryState)
    for fn in (inspect_repository, collect_repository_context, run_baseline, generate_issue_candidates,
               discovery_council, adjudicate_issues):
        g.add_node(fn.__name__, fn)
    g.add_edge(START, "inspect_repository")
    chain = ["inspect_repository", "collect_repository_context", "run_baseline", "generate_issue_candidates",
             "discovery_council", "adjudicate_issues"]
    for a, b in zip(chain, chain[1:]):
        g.add_edge(a, b)
    g.add_edge("adjudicate_issues", END)
    return g.compile(checkpointer=_checkpointer(settings))


def save_issues(db, commit, record_votes=False):
    """Turn stored council output into issue records (also used by `scan --reclassify`, no model calls)."""
    cands = db.get(f"candidates:{commit}")
    council = db.get(f"council:{commit}")
    votes, adj = council["votes"], council["adjudication"]
    if record_votes:
        for voter, out in votes.items():
            for v in out["votes"]:
                db.add_vote("discovery", v["candidate_id"], voter, v | {"meta": out["meta"]})
    ids = []
    for c in cands:
        decision = next((d for d in adj["decisions"] if d["candidate_id"] == c["candidate_id"]), None)
        issue = discovery.make_issue(c, votes, decision, commit, c["candidate_id"] in council["reviewed"])
        old = db.issue(issue["issue_id"])
        if old and old["approval_status"] != "pending":  # never overwrite a human decision
            issue["approval_status"], issue["scope"] = old["approval_status"], old.get("scope", issue["scope"])
        if decision and record_votes:
            db.add_vote("adjudication", issue["issue_id"], "adjudicator", decision | {"meta": adj["meta"]})
        db.save_issue(issue)
        ids.append(issue["issue_id"])
    db.put("discovery:latest", {"commit": commit, "ran_at": now(), "issues": ids, "reviewed": council["reviewed"]})
    return ids


# ---------------------------------------------------------------- repair graph

def repair_graph(settings, provider, db, log=print):
    def ctx(state):
        issue = db.issue(state["issue_id"])
        index = db.get("index:latest")
        base = db.get(f"baseline:{issue['repository_commit']}")
        return issue, index, base

    def past(state):
        """Cross-run memory from the decision log: (failures, prompt note)."""
        failures = history.prior_failures(settings, state["issue_id"])
        return failures, history.prompt_note(failures)

    def await_issue_approval(state):
        decision = db.latest_decision(state["issue_id"], "issue")
        if decision != "approve":
            if decision == "reject":
                return {"status": "rejected"}
            log(f"[gate 1] {state['issue_id']} is waiting for maintainer approval "
                f"(python -m resilience_agent approve {state['issue_id']})")
            interrupt({"gate": "issue", "issue_id": state["issue_id"]})
            decision = db.latest_decision(state["issue_id"], "issue")
            if decision != "approve":
                return {"status": "rejected" if decision == "reject" else "awaiting_issue_approval"}
        issue = db.issue(state["issue_id"])
        log(f"[gate 1] approved by maintainer; scope = {issue.get('scope')}")
        return {"status": "approved", "commit": issue["repository_commit"], "attempt": 0,
                "weights": {k: 1.0 for k in repair.STRATEGIES}, "weights_history": [], "ranking_history": [],
                "verification_labels": [], "errors": []}

    def generate_repair_candidates(state):
        issue, index, base = ctx(state)
        archived = db.archive_repair_run(state["issue_id"])
        if archived:
            log(f"[repair] previous run archived as {archived}")
        ids = []
        failures, note = past(state)
        if failures:
            log(f"[repair] decision log: {sum(f['status'] == 'skip' for f in failures)} failed approach(es) to avoid, "
                f"{sum(f['status'] == 'retest' for f in failures)} expired (retest allowed)")
        for s in repair.STRATEGIES:
            try:
                c = repair.propose(provider, issue, index, base["gate"], s, log=log, history_note=note)
            except Exception as e:  # model failure is recorded, not hidden
                c = {"strategy": s, "strategy_name": repair.STRATEGIES[s][0], "proposal": {"diagnosis": "", "solution": "",
                     "edits": [], "risks": [], "tests_needed": [], "compatibility": "", "predicted_effects": []},
                     "changes": [], "diff": "", "valid": False, "problems": [f"model error: {e}"], "created_at": now()}
            c["cand_id"] = f"{s}1"
            c["status"] = "proposed" if c["valid"] else "invalid"
            db.save_candidate(state["issue_id"], c)
            log(f"[repair]   {c['cand_id']}: valid={c['valid']} files={[x['path'] for x in c['changes']]} {c['problems']}")
            ids.append(c["cand_id"])
        return {"candidate_ids": ids, "status": "proposed"}

    def _review_round(state, cands, round_no):
        issue, index, base = ctx(state)
        labels = repair.anonymize(cands, seed=f"{state['issue_id']}-{round_no}")
        db.put(f"labels:{state['issue_id']}:{round_no}", labels)  # identity map kept out of the prompts
        out = []
        note = past(state)[1]
        for reviewer in repair.STRATEGIES:
            try:
                rs = repair.review(provider, issue, cands, reviewer, labels, base["gate"], log, history_note=note)
            except Exception as e:
                log(f"[repair]   reviewer {reviewer} failed: {e}")
                state.setdefault("errors", []).append(f"reviewer {reviewer}: {e}")
                rs = []
            for r in rs:
                db.add_vote("repair-review", state["issue_id"], f"reviewer-{reviewer}", r | {"round": round_no})
            out += rs
        return out

    def cross_review_candidates(state):
        cands = [c for c in db.candidates(state["issue_id"]) if c["valid"]]
        reviews = _review_round(state, cands, 1)
        log(f"[repair] {len(reviews)} anonymous reviews collected")
        return {"status": "reviewed"}

    def revise_candidates(state):
        if settings.revision_rounds < 1:
            return {"status": "revised"}
        issue, index, base = ctx(state)
        reviews = [v["data"] for v in db.votes(state["issue_id"], "repair-review")]
        for c in [c for c in db.candidates(state["issue_id"]) if c["valid"] and c["status"] == "proposed"]:
            fb = repair.critique_for(c, reviews)
            if not fb or all(r["verdict"] == "accept" and not r["defects"] for r in reviews if r["cand_id"] == c["cand_id"]):
                continue
            try:
                new = repair.propose(provider, issue, index, base["gate"], c["strategy"], feedback=fb, prior=c, log=log,
                                     history_note=past(state)[1])
            except Exception as e:
                log(f"[repair]   revision of {c['cand_id']} failed: {e}")
                continue
            if new["valid"]:
                new["cand_id"] = f"{c['strategy']}{int(c['cand_id'][1:]) + 1}"
                new["status"], new["revises"] = "proposed", c["cand_id"]
                c["status"] = "superseded"
                db.save_candidate(state["issue_id"], c)
                db.save_candidate(state["issue_id"], new)
                log(f"[repair]   {c['cand_id']} revised -> {new['cand_id']}")
        # revised candidates are re-reviewed anonymously so ranking uses current patches
        fresh = [c for c in db.candidates(state["issue_id"]) if c["valid"] and c.get("revises")
                 and not any(v["data"]["cand_id"] == c["cand_id"] for v in db.votes(state["issue_id"], "repair-review"))]
        if fresh:
            pool = [c for c in db.candidates(state["issue_id"]) if c["valid"] and c["status"] == "proposed"]
            _review_round(state, pool, 2)
        return {"status": "revised"}

    def rank_repair_candidates(state):
        cands = db.candidates(state["issue_id"])
        reviews = [v["data"] for v in db.votes(state["issue_id"], "repair-review")]
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.history_max_age_days)
        archived = [c for c in db.archived_candidates(state["issue_id"]) if c.get("status") == "failed"
                    and datetime.fromisoformat(c.get("created_at") or now()) >= cutoff]  # expired -> retest
        failures = past(state)[0]
        for c in cands:  # a patch that failed in an earlier run of this issue is not tried again
            if c.get("status") != "proposed":
                continue
            dup = repair.failed_duplicate(c, archived)
            logged = None if dup else history.is_fresh_failure(c, failures)
            if dup or logged:
                why = (f"identical to {dup} from an earlier run, which failed verification" if dup else
                       f"identical to a patch that failed on {logged['at'][:10]} (decision log {logged['entry']})")
                c["status"], c["valid"] = "invalid", False
                c["problems"] = c.get("problems", []) + [why]
                db.save_candidate(state["issue_id"], c)
                log(f"[repair]   {c['cand_id']} rejected: {why}")
        table = repair.rank(cands, reviews, state["weights"])
        log("[repair] ranking: " + ", ".join(f"{r['cand_id']}={r['score']}" for r in table))
        if not table:
            return {"status": "no_candidates", "ranking_history": state["ranking_history"] + [table]}
        return {"selected": table[0]["cand_id"], "status": "selected", "ranking_history": state["ranking_history"] + [table]}

    def apply_selected_patch(state):
        issue, index, base = ctx(state)
        cand = next(c for c in db.candidates(state["issue_id"]) if c["cand_id"] == state["selected"])
        applied = repair.apply_in_worktree(settings, issue, cand, issue["repository_commit"])
        cand["apply"] = applied
        db.save_candidate(state["issue_id"], cand)
        log(f"[apply] {cand['cand_id']} committed on {applied['branch']} in {applied['worktree']}")
        return {"branch": applied["branch"], "patch": applied["patch"], "status": "applied"}

    def run_verification(state):
        issue, index, base = ctx(state)
        cand = next(c for c in db.candidates(state["issue_id"]) if c["cand_id"] == state["selected"])
        attempt = state["attempt"] + 1
        label = f"{state['issue_id']}-{cand['cand_id']}-attempt{attempt}"
        result = verification.run_suite(settings, cand["apply"]["worktree"], label,
                                        verification.package_modules(index), index["entrypoints"], log)
        g = repair.issue_gate(result, base, issue)
        db.add_verification(state["issue_id"], label, {"cand_id": cand["cand_id"], "result": result, "gate": g,
                                                       "apply": cand["apply"]})
        return {"attempt": attempt, "verification_labels": state["verification_labels"] + [label], "status": "verified"}

    def evaluate_verification(state):
        v = db.verifications(state["issue_id"])[-1]["data"]
        cands = db.candidates(state["issue_id"])
        cand = next(c for c in cands if c["cand_id"] == state["selected"])
        reviews = [x["data"] for x in db.votes(state["issue_id"], "repair-review")]
        passed = v["gate"]["passed"]
        weights = repair.update_weights(state["weights"], cand, reviews, passed)
        cand["status"] = "verified" if passed else "failed"
        cand["gate"] = {k: v["gate"][k] for k in ("passed", "reasons", "fully_green")}
        db.save_candidate(state["issue_id"], cand)
        log(f"[verify] {cand['cand_id']} gate {'PASSED' if passed else 'FAILED'}: {v['gate']['reasons'] or 'all criteria met'}"
            f"; fully green: {v['gate']['fully_green']}; weights -> {weights}")
        fb = ""
        if not passed:
            issue = db.issue(state["issue_id"])
            scope = issue.get("scope") or issue["affected_dependencies"]
            defects = [d for r in reviews if r["cand_id"] == cand["cand_id"] for d in r.get("defects", [])]
            fb = repair.failure_feedback(
                v["gate"]["full_gate"] | {"failures": v["gate"]["new_check_failures"] or v["gate"]["full_gate"]["failures"]},
                v["gate"]["comparison"], reasons=v["gate"]["reasons"],
                declarations=repair.manifest_declarations(cand["apply"]["worktree"], scope=scope), defects=defects)
        return {"weights": weights, "weights_history": state["weights_history"] + [weights],
                "status": "passed" if passed else "failed", "feedback": fb}

    def route_after_eval(state):
        if state["status"] == "passed":
            return "generate_exit_capsule"
        return "revise_or_retry" if state["attempt"] < settings.max_attempts else "generate_exit_capsule"

    def revise_or_retry(state):
        """Send the exact failure evidence back to the proposer of the failed patch; re-review the revision."""
        issue, index, base = ctx(state)
        failed = next(c for c in db.candidates(state["issue_id"]) if c["cand_id"] == state["selected"])
        log(f"[retry] attempt {state['attempt']} failed; feeding verifier evidence back to proposer {failed['strategy']}")
        db.put(f"feedback:{state['issue_id']}:{state['attempt']}", state["feedback"])
        try:
            new = repair.propose(provider, issue, index, base["gate"], failed["strategy"],
                                 feedback="The verifier ran your patch and it FAILED:\n" + state["feedback"],
                                 prior=failed, log=log, prior_failed=True, attempt=state["attempt"],
                                 history_note=past(state)[1])
        except Exception as e:
            log(f"[retry] revision failed: {e}")
            return {"status": "retry_failed"}
        n = 1 + max(int(c["cand_id"][1:]) for c in db.candidates(state["issue_id"]) if c["strategy"] == failed["strategy"])
        new["cand_id"], new["revises"] = f"{failed['strategy']}{n}", failed["cand_id"]
        dup = repair.failed_duplicate(new, db.candidates(state["issue_id"]) + db.archived_candidates(state["issue_id"]))
        if dup:
            new["valid"] = False
            new["problems"] = new["problems"] + [f"identical to {dup}, which already failed verification"]
        new["status"] = "proposed" if new["valid"] else "invalid"
        new["feedback_used"] = state["feedback"]
        db.save_candidate(state["issue_id"], new)
        log(f"[retry] new candidate {new['cand_id']} valid={new['valid']} {new['problems']}")
        if new["valid"]:
            pool = [c for c in db.candidates(state["issue_id"]) if c["valid"] and c["status"] == "proposed"]
            _review_round(state, pool, 100 + state["attempt"])
        return {"status": "retrying"}

    def generate_exit_capsule(state):
        cap = reports.build_capsule(settings, db, state["issue_id"], state)
        log(f"[capsule] {cap['status']} -> {cap['dir']}")
        return {"capsule": cap, "status": "verified_resolution" if cap["status"] == "VERIFIED_RESOLUTION" else "no_verified_solution"}

    def await_publication_approval(state):
        if state["status"] != "verified_resolution":
            return {"publication": "not eligible (no verified solution)"}
        decision = db.latest_decision(state["issue_id"], "publication")
        if decision is None:
            log(f"[gate 2] verified patch on {state['branch']} waits for publication approval "
                f"(python -m resilience_agent publish {state['issue_id']} | reject-publication)")
            interrupt({"gate": "publication", "issue_id": state["issue_id"], "branch": state["branch"]})
            decision = db.latest_decision(state["issue_id"], "publication")
        return {"publication": {"approve": "approved", "reject": "rejected"}.get(decision, "pending")}

    def prepare_pull_request(state):
        if state.get("publication") != "approved":
            return {"pr": {"status": "not prepared", "reason": state.get("publication")}}
        cap = state["capsule"]
        body = f"{cap['dir']}/pr.md"
        cmd = ["gh", "pr", "create", "--draft", "--head", state["branch"], "--title",
               f"{state['issue_id']}: dependency repair (resilience_agent)", "--body-file", body]
        pr = {"status": "prepared", "branch": state["branch"], "push": f"git push -u origin {state['branch']}",
              "command": " ".join(f'"{c}"' if " " in c else c for c in cmd), "body_file": body}
        if settings_flag("RA_ALLOW_PUBLISH"):
            push = subprocess.run(["git", "-C", str(settings.repo), "push", "-u", "origin", state["branch"]],
                                  capture_output=True, text=True)
            created = subprocess.run(cmd, cwd=settings.repo, capture_output=True, text=True) if push.returncode == 0 else None
            pr.update({"status": "created" if created and created.returncode == 0 else "failed",
                       "output": ((created.stdout + created.stderr) if created else push.stderr)[-800:]})
        (settings.dir(f"artifacts/capsules/{state['issue_id']}") / "pr.json").write_text(json.dumps(pr, indent=2))
        log(f"[pr] {pr['status']}: {pr['command']}")
        return {"pr": pr}

    def refresh_knowledge_graph(state):
        if state.get("publication") != "approved":
            return {"graph_refresh": {"status": "skipped (change not accepted)"}, "status": state["status"]}
        cand = next(c for c in db.candidates(state["issue_id"]) if c["cand_id"] == state["selected"])
        from pathlib import Path
        wt = Path(cand["apply"]["worktree"])
        before = db.get("index:latest")["external"].get("graphify", {})
        after = context.graphify_graph(settings, wt, settings.dir(f"artifacts/capsules/{state['issue_id']}/graphify-after"))
        info = {"before": {k: before.get(k) for k in ("nodes", "links", "built_at_commit")},
                "after": {k: after.get(k) for k in ("status", "nodes", "links", "built_at_commit", "out_dir")}}
        log(f"[graph] refreshed on accepted change: {info}")
        return {"graph_refresh": info, "status": "completed"}

    g = StateGraph(RepairState)
    for fn in (await_issue_approval, generate_repair_candidates, cross_review_candidates, revise_candidates,
               rank_repair_candidates, apply_selected_patch, run_verification, evaluate_verification, revise_or_retry,
               generate_exit_capsule, await_publication_approval, prepare_pull_request, refresh_knowledge_graph):
        g.add_node(fn.__name__, fn)
    g.add_edge(START, "await_issue_approval")
    g.add_conditional_edges("await_issue_approval", lambda s: "generate_repair_candidates" if s["status"] == "approved" else END)
    g.add_edge("generate_repair_candidates", "cross_review_candidates")
    g.add_edge("cross_review_candidates", "revise_candidates")
    g.add_edge("revise_candidates", "rank_repair_candidates")
    g.add_conditional_edges("rank_repair_candidates", lambda s: "apply_selected_patch" if s["status"] == "selected" else "generate_exit_capsule")
    g.add_edge("apply_selected_patch", "run_verification")
    g.add_edge("run_verification", "evaluate_verification")
    g.add_conditional_edges("evaluate_verification", route_after_eval)
    g.add_edge("revise_or_retry", "rank_repair_candidates")
    g.add_edge("generate_exit_capsule", "await_publication_approval")
    g.add_edge("await_publication_approval", "prepare_pull_request")
    g.add_edge("prepare_pull_request", "refresh_knowledge_graph")
    g.add_edge("refresh_knowledge_graph", END)
    return g.compile(checkpointer=_checkpointer(settings))


# ---------------------------------------------------------------- review graph

class ReviewState(TypedDict, total=False):
    review_id: str
    base: str
    head: str
    base_sha: str
    head_sha: str
    base_wt: str
    head_wt: str
    pr_description: str
    full_suite: bool
    gen_tests: int
    use_ai: bool
    status: str
    verdict: str
    capsule: dict


def review_graph(settings, provider, db, log=print):
    """prepare_review -> map_blast_radius -> structural_checks -> verify_base -> verify_head -> behaviour_diff
    -> intent_review -> decide_verdict -> generate_review_capsule. No human gate: review never changes the repo."""
    from pathlib import Path

    from . import behaviour, history, review, structure

    def key(state, part):
        return f"review:{state['review_id']}:{part}"

    def out_dir(state):
        return settings.dir(f"artifacts/reviews/{state['review_id']}")

    def prepare_review(state):
        b, h = review.resolve_ref(settings.repo, state["base"]), review.resolve_ref(settings.repo, state["head"])
        bwt = verification.make_worktree(settings, f"review-{b[:12]}", b)
        hwt = verification.make_worktree(settings, f"review-{h[:12]}", h)
        text = review.diff_text(settings.repo, b, h)
        (out_dir(state) / "diff.patch").write_text(text, encoding="utf-8")
        db.put(key(state, "diff"), review.parse_diff(text))
        log(f"[review] {state['base']}={b[:12]} -> {state['head']}={h[:12]}: {text.count(chr(10))} diff lines")
        return {"base_sha": b, "head_sha": h, "base_wt": str(bwt), "head_wt": str(hwt), "status": "prepared"}

    def graphs(state):
        out = []
        for side in ("base", "head"):
            g = review.codegraph(state[f"{side}_wt"], settings.exclude)
            gj = Path(out_dir(state)) / f"graphify-{side}" / "graph.json"
            if gj.exists():
                review.add_graphify_edges(g, gj)
            out.append(g)
        return out

    def map_blast_radius(state):
        for side in ("base", "head"):
            gr = context.graphify_graph(settings, Path(state[f"{side}_wt"]), out_dir(state) / f"graphify-{side}")
            log(f"[review] Graphify on {side}: {gr['status']}")
        bg, hg = graphs(state)
        covs = {s: review.coverage_contexts(settings, state[f"{s}_wt"], out_dir(state) / f"coverage-{s}", log=log)
                for s in ("base", "head")}
        facts = {s: review.tree_facts(state[f"{s}_wt"], settings.exclude) for s in ("base", "head")}
        blast = review.blast_radius(db.get(key(state, "diff")), bg, hg, covs["base"], covs["head"],
                                    facts["base"]["entrypoints"], facts["head"]["entrypoints"])
        db.put(key(state, "facts"), facts)
        db.put(key(state, "blast"), blast)
        log(f"[review] blast radius: {len(blast['changed'])} changed, {len(blast['direct_callers'])} direct, "
            f"{len(blast['indirect_callers'])} indirect callers, {len(blast['affected_tests'])} tests, "
            f"coverage {blast['coverage']['percent']}%")
        return {"status": "mapped"}

    def structural_checks(state):
        bg, hg = graphs(state)
        facts = db.get(key(state, "facts"))
        res = structure.check(settings, state["base_wt"], state["head_wt"], bg, hg, facts, db.get(key(state, "diff")),
                              out_dir(state) / "structure.json")
        db.put(key(state, "structure"), res)
        log(f"[review] structure: {len(res['api']['breaking'])} API breaks, {len(res['cycles']['new'])} new cycles, "
            f"{len(res['layers']['new'])} layer violations")
        return {"status": "structure_checked"}

    def verify(state, side):
        facts = db.get(key(state, "facts"))[side]
        sel = review.select_tests(db.get(key(state, "blast")), state["base_wt"], state["head_wt"], state.get("full_suite"))
        tests = sel[0] if side == "base" else sel[1]
        log(f"[review] verifying {side} ({'full suite' if tests is None else f'{len(tests)} affected test file(s)'})")
        r = verification.run_suite(settings, state[f"{side}_wt"], f"{state['review_id']}-{side}", facts["package_modules"],
                                   facts["entrypoints"], log, venv_root=settings.dir("venvs-review"),
                                   reinstall_project=True, pytest_args=tests or ())
        r["selection"] = tests
        db.put(key(state, side), r)
        return {"status": f"verified_{side}"}

    def verify_base(state):
        return verify(state, "base")

    def verify_head(state):
        return verify(state, "head")

    def behaviour_diff(state):
        bg, hg = graphs(state)
        res = behaviour.run(settings, provider if state.get("use_ai") else None, state, bg, hg,
                            db.get(key(state, "blast")), db.get(key(state, "base")), db.get(key(state, "head")),
                            out_dir(state) / "behaviour", gen_tests=state.get("gen_tests", 0), log=log)
        db.put(key(state, "behaviour"), res)
        log(f"[review] behaviour: {len(res['diffs'])} differing outputs over {res['inputs_replayed']} recorded inputs")
        return {"status": "behaviour_checked"}

    def intent(state):
        if not (state.get("pr_description") and state.get("use_ai")):
            db.put(key(state, "intent"), {"label": "AI", "status": "skipped",
                                          "reason": "no PR description" if not state.get("pr_description") else "AI disabled"})
            return {"status": "intent_skipped"}
        try:
            res = review.intent_review(provider, state["pr_description"],
                                       (out_dir(state) / "diff.patch").read_text(encoding="utf-8"),
                                       db.get(key(state, "blast")), db.get(key(state, "structure")), log)
        except Exception as e:  # model failure is recorded, never fatal for a deterministic review
            res = {"label": "AI", "status": "failed", "reason": f"{type(e).__name__}: {e}"}
        db.put(key(state, "intent"), res)
        return {"status": "intent_reviewed"}

    def decide_verdict(state):
        base, head = db.get(key(state, "base")), db.get(key(state, "head"))
        cmp = verification.compare(base, head)
        db.put(key(state, "comparison"), cmp)
        v, reasons = review.verdict(base, head, cmp, db.get(key(state, "blast")), db.get(key(state, "structure")),
                                    db.get(key(state, "behaviour")))
        db.put(key(state, "verdict"), {"verdict": v, "reasons": reasons})
        log(f"[review] verdict: {v}")
        return {"verdict": v, "status": "decided"}

    def generate_review_capsule(state):
        parts = {p: db.get(key(state, p)) for p in ("blast", "structure", "base", "head", "comparison", "behaviour",
                                                    "intent", "verdict")}
        entry = history.review_log(settings, db, state, parts)
        cap = reports.build_review_capsule(settings, state, parts, entry)
        log(f"[capsule] {cap['verdict']} -> {cap['dir']}")
        return {"capsule": cap, "status": "completed"}

    g = StateGraph(ReviewState)
    nodes = [prepare_review, map_blast_radius, structural_checks, verify_base, verify_head, behaviour_diff,
             ("intent_review", intent), decide_verdict, generate_review_capsule]
    names = []
    for n in nodes:
        name, fn = n if isinstance(n, tuple) else (n.__name__, n)
        g.add_node(name, fn)
        names.append(name)
    g.add_edge(START, names[0])
    for a, b in zip(names, names[1:]):
        g.add_edge(a, b)
    g.add_edge(names[-1], END)
    return g.compile(checkpointer=_checkpointer(settings))


def settings_flag(name):
    import os
    return os.environ.get(name) == "1"


def drive(graph, thread_id, initial):
    """Start, resume after a crash, or resume after a human gate - whichever applies to this thread."""
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
    snap = graph.get_state(config)
    if snap.next:
        if any(t.interrupts for t in snap.tasks):
            graph.invoke(Command(resume="checked"), config)
        else:
            graph.invoke(None, config)
    else:
        graph.invoke(initial, config)
    snap = graph.get_state(config)
    waiting = [i.value for t in snap.tasks for i in t.interrupts]
    return snap.values, waiting
