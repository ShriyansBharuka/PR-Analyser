"""Decision Log: what was tried, what happened, what was NOT done and why - across runs.

Every reason points at a real artifact (a verification result or log, a checker output saved under raw/, or a
human approval row). `check_refs` refuses to save an entry whose references do not exist. AI-written text is
only allowed in the separate `ai_summary` field. Entries live in .resilience/history/<subject>/ as JSON + Markdown;
raw checker outputs sit next to them in <entry>-raw/, separate from the summary.
"""
import hashlib
import importlib.metadata
import json
import platform
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .storage import now
from .verification import scrub

Label = Literal["OBSERVED", "VERIFIED", "AI", "HUMAN"]


class Attempt(BaseModel):
    patch_summary: str
    diff_sha: str = ""
    result: Literal["passed", "failed", "invalid", "not_run"]
    reason: str
    label: Label
    ref: str = Field(min_length=1, description="artifact backing the reason: file path or approval:<id>")
    at: str = ""


class NotDone(BaseModel):
    item: str
    reason: Literal["out_of_scope", "NEEDS_INVESTIGATION", "too_risky", "NO_VERIFIED_SOLUTION"]
    detail: str = ""
    ref: str = Field(min_length=1)


class GateDecision(BaseModel):
    gate: str
    decision: str
    actor: str
    note: str = ""
    at: str
    ref: str = Field(min_length=1)
    label: Label = "HUMAN"


class DecisionLog(BaseModel):
    entry_id: str
    kind: Literal["repair", "review"]
    subject: str
    repository: str = ""
    commit: str = ""
    status: str
    verdict: str
    attempts: list[Attempt] = Field(default_factory=list)
    not_done: list[NotDone] = Field(default_factory=list)
    gate_decisions: list[GateDecision] = Field(default_factory=list)
    created_at: str
    tools: dict = Field(default_factory=dict)
    models: list[dict] = Field(default_factory=list)
    ai_summary: str | None = Field(default=None, description="AI-written; never used as a reason")


def diff_sha(diff):
    return hashlib.sha1(diff.strip().encode()).hexdigest()[:16] if diff and diff.strip() else ""


def tool_versions():
    def ver(name):
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return None

    def cli(exe):
        p = shutil.which(exe)
        if not p:
            return None
        out = subprocess.run([p, "--version"], capture_output=True, text=True)
        return out.stdout.strip() or None
    return {"python": platform.python_version(), "git": cli("git"), "uv": cli("uv"),
            **{n: ver(n) for n in ("langgraph", "pydantic", "griffe", "graphifyy")}}


def check_refs(entry: DecisionLog, db=None):
    """Every ref must exist: a file/folder on disk, or an approval row in the database."""
    approvals = {f"approval:{a['id']}" for a in (db.approvals(entry.subject) if db else [])}
    bad = []
    for item in [*entry.attempts, *entry.not_done, *entry.gate_decisions]:
        ok = item.ref in approvals if item.ref.startswith("approval:") else Path(item.ref).exists()
        if not ok:
            bad.append(item.ref)
    if bad:
        raise ValueError(f"decision log {entry.entry_id}: reasons reference missing artifacts: {bad}")


def save(settings, entry: DecisionLog, db=None):
    check_refs(entry, db)
    folder = settings.dir(f"history/{_safe(entry.subject)}")
    data = json.loads(scrub(entry.model_dump_json(indent=2)))
    for key in ("attempts", "not_done", "gate_decisions"):  # refs are paths/ids we wrote: keep them resolvable
        for item, orig in zip(data[key], getattr(entry, key)):
            item["ref"] = orig.ref
    entry = DecisionLog.model_validate(data)  # scrubbed copy must still be a valid entry
    (folder / f"{entry.entry_id}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    (folder / f"{entry.entry_id}.md").write_text(scrub(render_md(entry)), encoding="utf-8")
    return folder / f"{entry.entry_id}.json"


def _safe(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)


def raw_dir(settings, subject, entry_id):
    return settings.dir(f"history/{_safe(subject)}/{entry_id}-raw")


def load(settings, subject):
    folder = settings.work / "history" / _safe(subject)
    out = []
    for p in sorted(folder.glob("*.json")) if folder.exists() else []:
        try:
            out.append(DecisionLog.model_validate_json(p.read_text(encoding="utf-8")))
        except ValueError:
            continue  # a corrupt entry is skipped, never trusted
    return out


def prior_failures(settings, subject, max_age_days=None):
    """Failed approaches from earlier runs. Fresh ones are 'skip' (do not repeat), older ones 'retest'."""
    max_age = timedelta(days=settings.history_max_age_days if max_age_days is None else max_age_days)
    cutoff = datetime.now(timezone.utc) - max_age
    out = []
    for e in load(settings, subject):
        for a in e.attempts:
            if a.result not in ("failed", "invalid"):
                continue
            at = datetime.fromisoformat(a.at or e.created_at)
            out.append({"entry": e.entry_id, "diff_sha": a.diff_sha, "patch_summary": a.patch_summary,
                        "reason": a.reason, "ref": a.ref, "at": a.at or e.created_at,
                        "status": "skip" if at >= cutoff else "retest"})
    return out


def prompt_note(failures):
    """Text block for proposers and reviewers. Only verifier/checker reasons, never AI summaries."""
    skip = [f for f in failures if f["status"] == "skip"]
    retest = [f for f in failures if f["status"] == "retest"]
    lines = []
    if skip:
        lines.append("APPROACHES THAT ALREADY FAILED IN EARLIER RUNS (do not repeat them):")
        lines += [f"- {f['patch_summary'][:200]} -> {f['reason'][:240]}" for f in skip[:8]]
    if retest:
        lines.append("Older failed approaches (results may be stale; retrying is allowed if justified):")
        lines += [f"- {f['patch_summary'][:200]} -> {f['reason'][:160]}" for f in retest[:4]]
    return "\n".join(lines)


def is_fresh_failure(cand, failures):
    sha = diff_sha(cand.get("diff", ""))
    return next((f for f in failures if f["status"] == "skip" and sha and f["diff_sha"] == sha), None)


def render_md(e: DecisionLog):
    L = [f"# Decision log - {e.subject} ({e.kind})", "",
         f"**Status:** {e.status}  |  **Verdict:** {e.verdict}  |  {e.created_at}  |  commit `{e.commit[:12]}`", "",
         "Every reason links to the artifact it comes from. Raw outputs are in the `-raw` folder, not here.", "",
         "## Attempts"]
    L += [f"- [{a.label}] {a.result}: {a.patch_summary[:160]} - {a.reason[:300]} (`{a.ref}`)" for a in e.attempts] or ["- none"]
    L += ["", "## Not done"]
    L += [f"- {n.item}: **{n.reason}** {n.detail[:200]} (`{n.ref}`)" for n in e.not_done] or ["- nothing"]
    L += ["", "## Human gate decisions"]
    L += [f"- [HUMAN] {g.gate}: {g.decision} by {g.actor} at {g.at} {g.note} (`{g.ref}`)" for g in e.gate_decisions] or ["- none"]
    L += ["", "## Tools and models", f"- tools: {e.tools}"] + [f"- model: {m}" for m in e.models]
    if e.ai_summary:
        L += ["", "## AI summary (AI, not evidence)", e.ai_summary]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- builders

def _models(records):
    return [json.loads(m) for m in sorted({json.dumps({k: (r.get("meta") or {}).get(k)
                                                       for k in ("provider", "model", "digest", "fake")})
                                           for r in records if r.get("meta")})]


def repair_log(settings, db, issue_id, state, status, verdict):
    issue = db.issue(issue_id)
    entry_id = f"repair-{re.sub(r'[^0-9]', '', now())}"
    raw = raw_dir(settings, issue_id, entry_id)
    verifs = {v["data"]["cand_id"]: v for v in db.verifications(issue_id)}
    attempts = []
    for c in db.candidates(issue_id):
        summary = (c.get("proposal") or {}).get("solution") or "(no solution text)"
        v = verifs.get(c["cand_id"])
        if v:
            ref = str(Path(v["data"]["result"]["logs"]).parent / "result.json")
            passed = v["data"]["gate"]["passed"]
            attempts.append(Attempt(patch_summary=summary, diff_sha=diff_sha(c.get("diff")), label="VERIFIED",
                                    result="passed" if passed else "failed", ref=ref, at=v["created_at"],
                                    reason="; ".join(v["data"]["gate"]["reasons"]) or "all gate criteria met"))
            continue
        checker = raw / f"{c['cand_id']}-checker.json"  # validator output, persisted as the artifact of record
        checker.write_text(scrub(json.dumps({"cand_id": c["cand_id"], "status": c.get("status"),
                                             "problems": c.get("problems", []), "diff": c.get("diff", "")}, indent=2)),
                           encoding="utf-8")
        if c.get("problems"):
            attempts.append(Attempt(patch_summary=summary, diff_sha=diff_sha(c.get("diff")), result="invalid",
                                    reason="; ".join(c["problems"]), label="OBSERVED", ref=str(checker),
                                    at=c.get("created_at", "")))
        else:
            attempts.append(Attempt(patch_summary=summary, diff_sha=diff_sha(c.get("diff")), result="not_run",
                                    reason=f"status {c.get('status')}: not selected for verification",
                                    label="OBSERVED", ref=str(checker), at=c.get("created_at", "")))
    not_done = []
    last = db.verifications(issue_id)[-1] if db.verifications(issue_id) else None
    last_ref = str(Path(last["data"]["result"]["logs"]).parent / "result.json") if last else None
    if verdict == "NO_VERIFIED_SOLUTION":
        not_done.append(NotDone(item=issue_id, reason="NO_VERIFIED_SOLUTION",
                                detail=f"{len(verifs)} verified attempt(s), none passed the gate",
                                ref=last_ref or str(raw)))
    for t in (last["data"]["gate"].get("pre_existing_out_of_scope", []) if last else []):
        not_done.append(NotDone(item=f"{t['test']} ({t['python']}/{t['mode']})", reason="out_of_scope",
                                detail="failed before the change and is outside the approved scope", ref=last_ref))
    if issue.get("classification") == "NEEDS_INVESTIGATION":
        ev = raw / "issue-evidence.json"
        ev.write_text(scrub(json.dumps({"evidence": issue["evidence"], "rule_notes": issue["adjudication"].get("rule_notes")},
                                       indent=2)), encoding="utf-8")
        not_done.append(NotDone(item=f"{issue_id} classification", reason="NEEDS_INVESTIGATION",
                                detail="evidence rules did not support the issue", ref=str(ev)))
    gates = [GateDecision(gate=a["gate"], decision=a["decision"], actor=a["actor"], note=a["note"] or "",
                          at=a["created_at"], ref=f"approval:{a['id']}") for a in db.approvals(issue_id)]
    entry = DecisionLog(entry_id=entry_id, kind="repair", subject=issue_id, status=status, verdict=verdict,
                        repository=str(settings.repo), commit=issue["repository_commit"], attempts=attempts,
                        not_done=not_done, gate_decisions=gates, created_at=now(), tools=tool_versions(),
                        models=_models([c for c in db.candidates(issue_id)]))
    path = save(settings, entry, db)
    return entry.model_dump() | {"path": str(path)}


def review_log(settings, db, state, parts):
    entry_id = f"review-{re.sub(r'[^0-9]', '', now())}"
    raw = raw_dir(settings, state["review_id"], entry_id)
    attempts = []
    for side in ("base", "head"):
        r = parts[side]
        ref = str(Path(r["logs"]).parent / "result.json")
        bad = [f"py{e['python']}/{e['mode']}" for e in r["matrix"] if e["status"] != "passed"]
        attempts.append(Attempt(patch_summary=f"verifier on {side} {state[f'{side}_sha'][:12]}", label="VERIFIED",
                                result="passed" if not bad else "failed", ref=ref, at=r["ran_at"],
                                reason=f"failing environments: {', '.join(bad)}" if bad else "every environment passed"))
    struct_ref = parts["structure"]["ref"]
    attempts.append(Attempt(patch_summary="structural checks (griffe, import cycles, layers, packaging)",
                            result="passed" if not parts["structure"]["api"]["breaking"] else "failed",
                            reason=f"{len(parts['structure']['api']['breaking'])} public API break(s), "
                                   f"{len(parts['structure']['cycles']['new'])} new cycle(s)",
                            label="OBSERVED", ref=struct_ref))
    not_done = []
    blast = parts["blast"]
    for side in ("base", "head"):
        d = blast["dynamic"][side]
        if d.get("status") != "ok":
            note = raw / f"coverage-{side}.json"
            note.write_text(scrub(json.dumps(d, indent=2)), encoding="utf-8")
            not_done.append(NotDone(item=f"coverage contexts on {side}", reason="NEEDS_INVESTIGATION",
                                    detail=str(d.get("detail") or "")[:200], ref=d.get("log") or str(note)))
    for g in (parts.get("behaviour") or {}).get("generated", {}).get("discarded", []):
        not_done.append(NotDone(item=f"generated test {g['test']}", reason="out_of_scope",
                                detail=f"discarded: {g['why']}", ref=g["ref"]))
    if blast["coverage"]["available"]:
        for q in blast["coverage"]["unreached"][:20]:
            not_done.append(NotDone(item=f"{q} not executed by any test", reason="NEEDS_INVESTIGATION",
                                    detail="blast-radius function without test coverage",
                                    ref=blast["dynamic"]["head"].get("log") or struct_ref))
    intent = parts.get("intent") or {}
    entry = DecisionLog(entry_id=entry_id, kind="review", subject=state["review_id"], repository=str(settings.repo),
                        commit=state["head_sha"], status="completed", verdict=parts["verdict"]["verdict"],
                        attempts=attempts, not_done=not_done, created_at=now(), tools=tool_versions(),
                        models=_models([intent]) if intent.get("meta") else [],
                        ai_summary=intent.get("summary"))
    path = save(settings, entry, db)
    return entry.model_dump() | {"path": str(path)}
