"""End-to-end LangGraph repair flow on a throwaway repo with the FAKE provider and REAL verification
(uv venv + pytest). Proves gating, interrupt/resume persistence, worktree isolation and the capsule."""
import json

from resilience_agent import context, discovery, verification, workflow
from resilience_agent.llm import FakeProvider


def responder(role, prompt, schema):
    if role.startswith("proposer"):
        return {"diagnosis": "manifests disagree", "solution": f"align manifests ({role})",
                "edits": [{"path": "requirements.txt", "find": "# no runtime deps", "replace": f"# aligned by {role}"}],
                "risks": ["none"], "tests_needed": ["tests/test_core.py"], "compatibility": "py3.12+"}
    labels = sorted({w for w in prompt.split() if w.startswith("Proposal-")})
    return {"reviews": [{"proposal_label": l.strip(":#"), "scores": {k: 4 for k in (
        "correctness", "compatibility", "python_support", "regression_risk", "maintainability", "test_coverage",
        "complexity", "safety", "evidence")}, "defects": [], "verdict": "accept", "summary": "fine"} for l in labels]}


def test_full_repair_graph_with_gates(settings, tiny_repo, monkeypatch):
    from conftest import git
    (tiny_repo / "requirements.txt").write_text("# no runtime deps\n", encoding="utf-8")
    (tiny_repo / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1"\ndependencies = []\n'
                                              '[tool.setuptools]\npackages = ["demo"]\n', encoding="utf-8")
    (tiny_repo / "demo" / "core.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    git(tiny_repo, "commit", "-qam", "simplify")
    monkeypatch.setattr(context, "graphify_graph", lambda *a, **k: {"status": "unavailable", "detail": "stubbed in test"})
    db = settings.db()
    ix = context.index(settings, db, with_external=False, log=lambda *_: None)
    base = verification.baseline(settings, db, ix, log=lambda *_: None)
    assert base["matrix"][0]["checks"]["install"]["ok"], base["matrix"][0]["checks"]["install"]["stderr_tail"]
    cand = {"candidate_id": "RA-demo", "dependency": "demo", "categories": ["dependency-inconsistency"],
            "evidence": [{"id": "E1", "kind": "source", "text": "x", "source": "requirements.txt:1"}],
            "files": [], "symbols": [], "target_tests": [], "score": 1}
    db.save_issue(discovery.make_issue(cand, {}, None, ix["meta"]["commit"], council_reviewed=False))

    fake = FakeProvider(responder)
    g = workflow.repair_graph(settings, fake, db, log=lambda *_: None)
    values, waiting = workflow.drive(g, "t1", {"issue_id": "RA-demo"})
    assert waiting and waiting[0]["gate"] == "issue" and not fake.calls  # nothing ran before approval

    db.record_approval("RA-demo", "issue", "approve", actor="test-fixture")  # fixture-only approval
    g = workflow.repair_graph(settings, fake, db, log=lambda *_: None)  # fresh process: state from checkpoint
    values, waiting = workflow.drive(g, "t1", {"issue_id": "RA-demo"})
    assert values["status"] == "verified_resolution", values
    assert waiting and waiting[0]["gate"] == "publication"
    assert git(tiny_repo, "rev-parse", "--abbrev-ref", "HEAD") in ("master", "main")
    assert (tiny_repo / "requirements.txt").read_text() == "# no runtime deps\n"  # primary tree untouched
    cap = json.loads((tiny_repo / ".resilience/artifacts/capsules/RA-demo/capsule.json").read_text())
    assert cap["status"] == "VERIFIED_RESOLUTION" and cap["VERIFIED"]["attempts"][0]["gate"]["passed"]
    assert any(m["fake"] for m in cap["models"]) or cap["models"] == []

    db.record_approval("RA-demo", "publication", "approve", actor="test-fixture")
    values, waiting = workflow.drive(g, "t1", {"issue_id": "RA-demo"})
    assert values["pr"]["status"] == "prepared" and "gh pr create" in values["pr"]["command"]
    assert not waiting
