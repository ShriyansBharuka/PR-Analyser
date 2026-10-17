"""Dependency Exit Capsule (Markdown + JSON) and PR-ready summary. Every section is labelled
OBSERVED (read from files/metadata), VERIFIED (executed), or AI (model-generated)."""
import json
from pathlib import Path

from . import history
from .storage import now
from .verification import scrub

VERDICTS = ("SAFE", "REGRESSION_FOUND", "FIXED", "NEEDS_HUMAN", "UNVERIFIABLE", "NO_VERIFIED_SOLUTION")


def _env_table(result):
    rows = ["| Environment | Status | " + " | ".join(sorted({k for e in result["matrix"] for k in e.get("checks", {})})) + " |"]
    keys = sorted({k for e in result["matrix"] for k in e.get("checks", {})})
    rows.append("|" + "---|" * (len(keys) + 2))
    for e in result["matrix"]:
        cells = [("pass" if e["checks"][k]["ok"] else "**FAIL**") if k in e.get("checks", {}) else "-" for k in keys]
        rows.append(f"| py{e['python']} / {e['mode']} | {e['status']} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


def _failed_tests(result):
    out = []
    for e in result["matrix"]:
        for n, t in {**e.get("tests", {}), **e.get("acceptance_tests", {})}.items():
            if t["outcome"] in ("failed", "error"):
                out.append(f"- py{e['python']}/{e['mode']}: `{n}` {t['outcome']}: {t.get('message', '')[:160]}")
    return "\n".join(out) or "- none"


def build_capsule(settings, db, issue_id, state):
    issue = db.issue(issue_id)
    index = db.get("index:latest")
    base = db.get(f"baseline:{issue['repository_commit']}") or db.get("baseline:latest")
    cands = db.candidates(issue_id)
    reviews = [v["data"] for v in db.votes(issue_id, "repair-review")]
    verifs = db.verifications(issue_id)
    selected = next((c for c in cands if c["cand_id"] == state.get("selected")), None)
    final = verifs[-1]["data"] if verifs else None
    verified = bool(final and final["gate"]["passed"])
    scope = issue.get("scope") or issue["affected_dependencies"]
    scan = {p["name"]: p for p in index["scan"]["packages"]}
    usage = index["usages"]["packages"]
    out_dir = settings.dir(f"artifacts/capsules/{issue_id}")
    models = sorted({json.dumps({k: v.get("meta", {}).get(k) for k in ("provider", "model", "digest", "fake")})
                     for v in [*db.votes(issue_id)] for v in [v["data"]] if v.get("meta")})
    status = "VERIFIED_RESOLUTION" if verified else "NO_VERIFIED_SOLUTION"
    verdict = "FIXED" if verified else "NO_VERIFIED_SOLUTION"
    log_entry = history.repair_log(settings, db, issue_id, state, status, verdict)
    capsule = {
        "capsule_version": 2, "generated_at": now(), "issue_id": issue_id,
        "status": status, "verdict": verdict,
        "maintainer_status": state.get("publication", "awaiting maintainer decision"),
        "repository": {"path": index["meta"]["path"], "remote": index["meta"]["remote"],
                       "baseline_commit": issue["repository_commit"], "branch": index["meta"]["branch"]},
        "models": [json.loads(m) for m in models],
        "OBSERVED": {
            "original_dependencies": {d: {"declarations": scan.get(d, {}).get("sources", []),
                                          "pinned": scan.get(d, {}).get("pinned_version"),
                                          "pypi": scan.get(d, {}).get("pypi"),
                                          "risk_flags": scan.get(d, {}).get("risk_flags")} for d in scope},
            "usage_sites": {d: usage.get(d, {}).get("usages", []) for d in scope},
            "api_symbols": {d: usage.get(d, {}).get("api_symbols", []) for d in scope},
            "issue_evidence": issue["evidence"],
        },
        "AI": {
            "discovery_votes": issue["discovery_votes"], "adjudication": issue["adjudication"],
            "candidates": [{k: c.get(k) for k in ("cand_id", "strategy_name", "valid", "problems", "status", "score")}
                           | {"diagnosis": c["proposal"]["diagnosis"], "solution": c["proposal"]["solution"],
                              "risks": c["proposal"]["risks"], "tests_needed": c["proposal"]["tests_needed"],
                              "compatibility": c["proposal"]["compatibility"]} for c in cands],
            "peer_reviews": [{k: r.get(k) for k in ("reviewer", "cand_id", "proposal_label", "verdict", "scores", "defects", "summary")}
                             for r in reviews],
            "ranking_history": state.get("ranking_history", []),
            "voter_weights_history": state.get("weights_history", []),
        },
        "VERIFIED": {
            "baseline": {"gate": base.get("gate"), "logs": base.get("logs"),
                         "matrix": [{k: e.get(k) for k in ("python", "mode", "status", "resolved")} for e in base["matrix"]]},
            "attempts": [{"label": v["label"], "cand_id": v["data"]["cand_id"], "gate": v["data"]["gate"],
                          "logs": v["data"]["result"]["logs"], "commit": v["data"]["apply"]["commit"],
                          "branch": v["data"]["apply"]["branch"]} for v in verifs],
        },
        "selected": {"cand_id": selected["cand_id"], "strategy": selected["strategy_name"],
                     "branch": state.get("branch"), "patch_file": str(out_dir / "selected.patch")} if selected else None,
        "rollback": (f"The change exists only on branch {state.get('branch')} (worktree). Rollback before merge: "
                     f"`git branch -D {state.get('branch')}`. After merge: `git revert <merge-commit>` and reinstall "
                     f"with the previous manifests (baseline commit {issue['repository_commit'][:12]}).") if selected else "No change applied.",
        "unresolved": (final["gate"]["reasons"] if final and not verified else []) +
                      [f"pre-existing, out of scope: {t['test']} ({t['python']}/{t['mode']})"
                       for t in (final["gate"]["pre_existing_out_of_scope"] if final else [])],
        "gaps": ([f"Python {p} not tested (interpreter unavailable)" for p in final["gate"]["full_gate"]["missing_interpreters"]]
                 if final else []) + ["Isolation is reduced-trust local mode (git worktree + venv), not a container sandbox."],
        "decision_log": log_entry,
    }
    if selected:
        (out_dir / "selected.patch").write_text(state.get("patch", ""), encoding="utf-8")
    (out_dir / "capsule.json").write_text(scrub(json.dumps(capsule, indent=2, default=str)), encoding="utf-8")
    (out_dir / "capsule.md").write_text(scrub(render_md(capsule, issue, base, final, selected, state)), encoding="utf-8")
    (out_dir / "pr.md").write_text(scrub(pr_summary(capsule, issue, final, selected)), encoding="utf-8")
    return {"dir": str(out_dir), "status": capsule["status"], "verdict": verdict}


def render_md(c, issue, base, final, selected, state):
    scope = ", ".join(c["OBSERVED"]["original_dependencies"])
    L = [f"# Dependency Exit Capsule - {c['issue_id']}", "",
         f"**Verdict:** {c['verdict']}  |  **Status:** {c['status']}  |  **Maintainer:** {c['maintainer_status']}  |  generated {c['generated_at']}", "",
         "Labels: **OBSERVED** = read from repository files or package metadata; **VERIFIED** = produced by executing "
         "commands (logs linked); **AI** = generated by the open-weight model, not proof.", "",
         "## 1. Repository and model", f"- Repository: `{c['repository']['remote']}` at baseline commit "
         f"`{c['repository']['baseline_commit']}`",
         "- Models: " + "; ".join(f"{m['model']} via {m['provider']} (digest {(m.get('digest') or '?')[:12]}"
                                   f"{', FAKE' if m.get('fake') else ''})" for m in c["models"]), "",
         f"## 2. Original dependency information (OBSERVED) - {scope}"]
    for dep, d in c["OBSERVED"]["original_dependencies"].items():
        L.append(f"### {dep}")
        for s in d["declarations"]:
            L.append(f"- `{s['file']}:{s['line']}` -> `{s['name']}{s['spec']}` ({s['group']})")
        p = d.get("pypi") or {}
        if p and "error" not in p:
            L.append(f"- PyPI: latest {p['latest_version']}, last release {(p['last_release_date'] or '')[:10]}, "
                     f"releases since pin: {p.get('releases_since_pinned', 'n/a')}")
        flags = [k for k, v in (d.get("risk_flags") or {}).items() if v]
        L.append(f"- Risk flags: {', '.join(flags) or 'none'}")
    L += ["", "## 3. Why a migration was considered (OBSERVED + VERIFIED evidence)"]
    L += [f"- {x['id']} [{x['kind']}] {x['text']} - `{x['source']}`" for x in c["OBSERVED"]["issue_evidence"]]
    L += ["", "## 4. Usage sites (OBSERVED)"]
    for dep, uses in c["OBSERVED"]["usage_sites"].items():
        L += [f"- `{u['file']}:{u['line']}` `{u['code']}`" for u in uses] or [f"- {dep}: no imports found"]
        L.append(f"- API symbols used: {', '.join(c['OBSERVED']['api_symbols'][dep]) or 'none'}")
    for g in issue.get("maintainer_guidance", []):
        L += ["", f"**Maintainer guidance (HUMAN, {g['at']})** given to every proposer:", "", "> " + g["text"].replace("\n", "\n> ")]
    L += ["", "## 5. Discovery council (AI)",
          f"- Classification after evidence rules: **{issue['classification']}** ({issue['confidence_label']}), "
          f"reproduction: {issue['reproduction_status']}"]
    for v, x in issue["discovery_votes"].items():
        if x:
            L.append(f"- {v}: {x['verdict']} / {x['severity']} - {x['rationale']}")
    L.append(f"- Adjudicator: {issue['adjudication'].get('rationale')}")
    L += [f"- Rule note: {n}" for n in issue["adjudication"].get("rule_notes", [])]
    L += ["", "## 6. Candidate approaches (AI)"]
    for k in c["AI"]["candidates"]:
        L.append(f"### {k['cand_id']} - {k['strategy_name']} (status: {k.get('status')}, valid: {k['valid']})")
        L += [f"- Diagnosis: {k['diagnosis']}", f"- Solution: {k['solution']}",
              f"- Risks: {'; '.join(k['risks']) or '-'}"]
        if k["problems"]:
            L.append(f"- Rejected by validation: {'; '.join(k['problems'])}")
    L += ["", "## 7. Anonymous peer review and ranking (AI + deterministic ranking)"]
    for r in c["AI"]["peer_reviews"]:
        L.append(f"- reviewer {r['reviewer']} on {r['proposal_label']} (= {r['cand_id']}): {r['verdict']} - "
                 f"{r['summary']} Defects: {'; '.join(r['defects']) or 'none'}")
    for i, rk in enumerate(c["AI"]["ranking_history"], 1):
        L.append(f"- Ranking round {i}: " + ", ".join(f"{x['cand_id']}={x['score']}" for x in rk))
    for i, w in enumerate(c["AI"]["voter_weights_history"], 1):
        L.append(f"- Voter weights after attempt {i}: {w}")
    L += ["", "## 8. Selected approach and actual code changes"]
    if selected:
        L += [f"- Selected: **{selected['cand_id']}** ({selected['strategy_name']}) on branch `{state.get('branch')}`",
              f"- Patch file: `{c['selected']['patch_file']}`", "", "```diff", state.get("patch", "")[:12000], "```"]
    else:
        L.append("- No candidate was selected.")
    L += ["", "## 9. Tests executed BEFORE the change (VERIFIED)", _env_table(base), "", "Failing:", _failed_tests(base),
          f"\nLogs: `{base.get('logs')}`", "", "## 10. Tests executed AFTER the change (VERIFIED)"]
    for v in c["VERIFIED"]["attempts"]:
        L.append(f"- Attempt `{v['label']}` candidate {v['cand_id']} commit `{(v['commit'] or '')[:12]}`: "
                 f"gate {'PASSED' if v['gate']['passed'] else 'FAILED'} {'; '.join(v['gate']['reasons'])} - logs `{v['logs']}`")
    if final:
        L += ["", _env_table(final["result"]), "", "Failing:", _failed_tests(final["result"])]
        L += ["", "## 11. Regression and failure findings (VERIFIED)",
              f"- Regressions vs baseline: {', '.join(final['gate']['comparison']['regressions']) or 'none'}",
              f"- Fixed vs baseline: {', '.join(final['gate']['comparison']['fixed']) or 'none'}",
              f"- Fully green (every check and test): {final['gate']['fully_green']}"]
    L += ["", "## 12. Remaining risks, gaps and unresolved concerns"]
    L += [f"- {x}" for x in c["unresolved"] + c["gaps"]] or ["- none recorded"]
    if selected:
        L += [f"- AI-predicted risks (unverified): {'; '.join(selected['proposal']['risks']) or '-'}"]
    L += ["", "## 13. Rollback", c["rollback"], "", "## 14. PR-ready summary", "See `pr.md` next to this file.",
          "", "## 15. Decision log", *decision_log_md(c["decision_log"]),
          "", "## Reproduce", "```", "python -m resilience_agent index", "python -m resilience_agent scan",
          f"python -m resilience_agent approve {c['issue_id']}", f"python -m resilience_agent repair {c['issue_id']}",
          f"python -m resilience_agent report {c['issue_id']}", "```"]
    return "\n".join(L) + "\n"


def pr_summary(c, issue, final, selected):
    if not selected:
        return f"# {issue['title']}\n\nNo verified repair. See capsule.md.\n"
    g = final["gate"] if final else {}
    return "\n".join([
        f"# {c['issue_id']}: {issue['title']}", "",
        "## Problem (observed and executed evidence)",
        *[f"- {x['text']}" for x in issue["evidence"] if x["kind"] == "executed"][:6], "",
        f"## Change ({selected['strategy_name']} strategy, chosen by the repair council)",
        selected["proposal"]["solution"], "",
        "## Verification (executed)",
        f"- Gate: {'PASSED' if g.get('passed') else 'FAILED'} {'; '.join(g.get('reasons', []))}",
        f"- Environments: {', '.join(g.get('full_gate', {}).get('environments_tested', []))}",
        f"- Fixed vs baseline: {', '.join(g.get('comparison', {}).get('fixed', [])) or 'none'}",
        f"- Regressions: {', '.join(g.get('comparison', {}).get('regressions', [])) or 'none'}",
        f"- Still failing (out of scope): {len(g.get('pre_existing_out_of_scope', []))}", "",
        "## Risks (AI-predicted, not verified)", *[f"- {r}" for r in selected["proposal"]["risks"]], "",
        "Generated by resilience_agent with an open-weight model; reviewed by a human before publication.", ""])


def decision_log_md(entry):
    L = [f"Saved to `{entry['path']}` (raw checker outputs in the `-raw` folder next to it)."]
    L += [f"- [{a['label']}] {a['result']}: {a['patch_summary'][:120]} - {a['reason'][:200]} (`{a['ref']}`)"
          for a in entry["attempts"]]
    L += [f"- NOT DONE: {n['item']} - **{n['reason']}** (`{n['ref']}`)" for n in entry["not_done"]]
    L += [f"- [HUMAN] {g['gate']}: {g['decision']} by {g['actor']} at {g['at']}" for g in entry["gate_decisions"]]
    return L


# ---------------------------------------------------------------- review capsule

def build_review_capsule(settings, state, parts, log_entry):
    out_dir = settings.dir(f"artifacts/reviews/{state['review_id']}")
    v = parts["verdict"]
    capsule = {
        "capsule_version": 2, "kind": "review", "generated_at": now(), "review_id": state["review_id"],
        "verdict": v["verdict"], "verdict_reasons": v["reasons"], "repository": str(settings.repo),
        "base": {"ref": state["base"], "commit": state["base_sha"]},
        "head": {"ref": state["head"], "commit": state["head_sha"]},
        "blast_radius": parts["blast"],
        "structure": parts["structure"],
        "tests": {"label": "VERIFIED", "base_selection": parts["base"].get("selection"),
                  "head_selection": parts["head"].get("selection"),
                  "base": [{k: e.get(k) for k in ("python", "mode", "status")} for e in parts["base"]["matrix"]],
                  "head": [{k: e.get(k) for k in ("python", "mode", "status")} for e in parts["head"]["matrix"]],
                  "comparison": parts["comparison"], "base_logs": parts["base"]["logs"], "head_logs": parts["head"]["logs"]},
        "behaviour": parts["behaviour"],
        "ai_review": parts["intent"],
        "decision_log": log_entry,
    }
    (out_dir / "capsule.json").write_text(scrub(json.dumps(capsule, indent=2, default=str)), encoding="utf-8")
    (out_dir / "capsule.md").write_text(scrub(render_review_md(capsule, parts)), encoding="utf-8")
    return {"dir": str(out_dir), "verdict": v["verdict"]}


def render_review_md(c, parts):
    b, s, beh, ai = c["blast_radius"], c["structure"], c["behaviour"] or {}, c["ai_review"] or {}
    cov = b["coverage"]
    L = [f"# Review Capsule - {c['review_id']}", "",
         f"**Verdict:** {c['verdict']}  |  base `{c['base']['ref']}` {c['base']['commit'][:12]} -> head "
         f"`{c['head']['ref']}` {c['head']['commit'][:12]}  |  generated {c['generated_at']}", "",
         "Labels: **OBSERVED** = read from code or git; **VERIFIED** = produced by running something (logs linked); "
         "**AI** = model opinion, never changes the verdict.", "", "## 1. Verdict reasons"]
    L += [f"- [{r['label']}] {r['text']} (`{r['ref']}`)" for r in c["verdict_reasons"]]
    L += ["", "## 2. Blast radius",
          "Coverage of affected functions [VERIFIED]: "
          + (f"{cov['reached']}/{cov['affected_functions']} ({cov['percent']}%)" if cov["available"]
             else "unavailable (coverage run failed; see decision log)"), "",
          "| Level | Item | Sources | Label |", "|---|---|---|---|"]
    for level in ("changed", "direct_callers", "indirect_callers", "affected_tests", "entry_points"):
        for x in b[level]:
            name = x.get("symbol") or x.get("test") or f"{x.get('name')} = {x.get('target')}"
            extra = f" ({x['change']})" if level == "changed" else ""
            unc = " *uncertain*" if x.get("uncertain") else ""
            L.append(f"| {level} | `{name}`{extra}{unc} | {', '.join(x.get('sources', []))} | {x['label']} |")
    if cov.get("unreached"):
        L.append("\nNot executed by any test: " + ", ".join(f"`{q}`" for q in cov["unreached"]))
    L += ["", "## 3. Structural changes (OBSERVED)"]
    L += [f"- API: {x['explanation']}" for x in s["api"]["breaking"]] or ["- no public API breaks"]
    L += [f"- API check could not load: {e}" for e in s["api"]["errors"]]
    L += [f"- NEW import cycle: {' -> '.join(cyc)}" for cyc in s["cycles"]["new"]]
    L += [f"- Layer rule broken: {x['rule']} via {' -> '.join(x['chain'])}" for x in s["layers"]["new"]]
    pk = s["packaging"]
    for k, vals in (("entry points", pk["entry_points"]), ("dependencies", pk["dependencies"])):
        for kind, items in vals.items():
            L += [f"- {k} {kind}: {i}" for i in items]
    L += [f"- new undeclared import: {x}" for x in pk["new_undeclared_imports"]]
    t = c["tests"]
    L += ["", "## 4. Tests: base vs head (VERIFIED)",
          f"Selection: base {t['base_selection'] or 'full suite'}, head {t['head_selection'] or 'full suite'}",
          "", "### Base", _env_table(parts["base"]), "", "### Head", _env_table(parts["head"]), "",
          f"- Regressions: {', '.join(t['comparison']['regressions']) or 'none'}",
          f"- Fixed: {', '.join(t['comparison']['fixed']) or 'none'}",
          f"- Logs: base `{t['base_logs']}`, head `{t['head_logs']}`"]
    L += ["", "## 5. Behaviour differences (VERIFIED)",
          f"{beh.get('inputs_replayed', 0)} recorded inputs replayed on {len(beh.get('pure_functions', []))} pure function(s)."]
    L += [f"- `{d['function']}({d['input']})`: base `{d['base']}` -> head `{d['head']}` (`{d['ref']}`)"
          for d in beh.get("diffs", [])] or ["- no differences"]
    L += [f"- acceptance test changed outcome: {x}" for x in beh.get("acceptance_changes", [])]
    gen = beh.get("generated", {})
    if gen.get("requested"):
        L.append(f"- AI-generated tests: {len(gen.get('kept', []))} kept (passed on base), "
                 f"{len(gen.get('discarded', []))} discarded")
        L += [f"  - fails on head [VERIFIED, test written by AI]: {x['test']} (`{x['ref']}`)" for x in gen.get("head_failures", [])]
    L += ["", "## 6. AI review of intent (AI - cannot change the verdict)"]
    if ai.get("status") == "ok":
        L += [f"- Stated intent: {ai['stated_intent']}", f"- Summary: {ai['summary']}"]
        L += [f"- MISMATCH ({m['severity']}): claims '{m['claim']}' but {m['observed']} ({', '.join(m['files'])})"
              for m in ai["mismatches"]] or ["- no mismatches reported"]
        L += [f"- dropped (cited no changed file): {m['claim']}" for m in ai.get("dropped", [])]
    else:
        L.append(f"- {ai.get('status', 'skipped')}: {ai.get('reason', '')}")
    L += ["", "## 7. Decision log", *decision_log_md(c["decision_log"]), "",
          "## Reproduce", "```", f"python -m resilience_agent --repo {c['repository']} review --base {c['base']['ref']} "
          f"--head {c['head']['ref']}", "```"]
    return "\n".join(L) + "\n"
