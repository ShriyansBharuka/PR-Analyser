"""Review graph end to end on fixture repos: REAL worktrees, venvs, pytest, coverage contexts and replay.
The model is the FAKE provider, so these prove the plumbing and the rules, not model quality."""
import json
from pathlib import Path

import pytest
from conftest import fixture_repo, review_settings

from resilience_agent import context, workflow
from resilience_agent.llm import FakeProvider
from resilience_agent.verification import find_python

pytestmark = pytest.mark.skipif(not find_python("3.12"), reason="needs a Python 3.12 interpreter for the verifier")


def responder(role, prompt, schema):
    if role == "intent-reviewer":
        return {"stated_intent": "refactor only", "summary": "also changes output",
                "mismatches": [{"claim": "no behaviour change", "observed": "normalise no longer lowercases",
                                "files": ["pkg/norm.py"], "severity": "high"}]}
    return {"code": "from pkg.norm import normalise\n\n\ndef test_gen():\n    assert normalise(' A ') == 'a'\n",
            "explanation": "pins lowercasing"}


def run_review(tmp_path, monkeypatch, case, **extra):
    monkeypatch.setattr(context, "graphify_graph", lambda *a, **k: {"status": "unavailable", "detail": "stubbed"})
    repo, base, head = fixture_repo(tmp_path, case)
    s = review_settings(repo)
    db = s.db()
    rid = f"review-{base[:8]}-{head[:8]}"
    g = workflow.review_graph(s, FakeProvider(responder), db, log=lambda *_: None)
    values, waiting = workflow.drive(g, rid, {"review_id": rid, "base": base, "head": head, "use_ai": True, **extra})
    cap = json.loads((Path(values["capsule"]["dir"]) / "capsule.json").read_text(encoding="utf-8"))
    return values, cap, s


def test_regression_is_verified_and_capsule_has_every_section(tmp_path, monkeypatch):
    values, cap, s = run_review(tmp_path, monkeypatch, "regression")
    assert values["status"] == "completed" and cap["verdict"] == "REGRESSION_FOUND"
    assert any("test_add" in r["text"] and r["label"] == "VERIFIED" for r in cap["verdict_reasons"])
    assert cap["tests"]["head_selection"] == ["tests/test_calc.py"]  # only the affected test file ran
    b = cap["blast_radius"]
    assert [c["symbol"] for c in b["changed"]] == ["pkg.calc.add"]
    assert b["affected_tests"][0]["sources"] == ["dynamic", "static"]
    assert b["coverage"]["percent"] == 100.0
    for k in ("blast_radius", "structure", "tests", "behaviour", "ai_review", "decision_log"):
        assert k in cap
    log = cap["decision_log"]
    assert Path(log["path"]).exists() and all(Path(a["ref"]).exists() for a in log["attempts"])
    assert (Path(values["capsule"]["dir"]) / "capsule.md").read_text(encoding="utf-8").count("REGRESSION") >= 1


def test_behaviour_change_found_by_replay_and_generated_test(tmp_path, monkeypatch):
    values, cap, s = run_review(tmp_path, monkeypatch, "behaviour_change", gen_tests=1,
                                pr_description="Refactor normalise; no behaviour change.")
    beh = cap["behaviour"]
    assert beh["pure_functions"] == ["pkg.norm.normalise"]
    assert beh["diffs"] and beh["diffs"][0]["base"] == "return 'mixed case'" and beh["diffs"][0]["head"] == "return 'Mixed Case'"
    gen = beh["generated"]
    assert [k["test"] for k in gen["kept"]] == ["test_gen_pkg_norm_normalise.py"] and gen["head_failures"]
    assert Path(gen["dir"]).is_relative_to(s.work / "generated_tests")
    assert cap["verdict"] == "NEEDS_HUMAN"  # existing tests pass; only behaviour evidence flags it
    assert cap["ai_review"]["mismatches"][0]["label"] == "AI"
    assert all(r["label"] != "AI" for r in cap["verdict_reasons"])  # AI never decides the verdict
