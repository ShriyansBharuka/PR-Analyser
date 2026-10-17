import json

from resilience_agent import context, discovery
from resilience_agent.llm import FakeProvider


def test_index_records_commit_conflicts_and_usages(settings, tiny_repo):
    from conftest import git
    r = context.index(settings, with_external=False, log=lambda *_: None)
    assert r["meta"]["commit"] == git(tiny_repo, "rev-parse", "HEAD")
    old = next(p for p in r["scan"]["packages"] if p["name"] == "oldlib")
    assert old["risk_flags"]["specs_differ"]
    u = r["usages"]["packages"]["oldlib"]
    assert {x["file"] for x in u["usages"]} == {"demo/core.py"}
    assert u["private_api"][0]["symbol"] == "oldlib.utils._hidden"
    facts = settings.db().facts(commit=r["meta"]["commit"])
    assert any(f["kind"] == "declaration" and f["source"] == "requirements.txt" for f in facts)
    assert all(f["priority"] for f in facts)


def test_context_queries(settings):
    c = context.Context(context.index(settings, with_external=False, log=lambda *_: None))
    assert c.modules_depending_on("oldlib") == ["demo/core.py"]
    assert "requirements.txt" in c.files_for_migration("oldlib")
    assert c.find_feature("value")[0]["name"] == "value"


def test_external_sources_degrade_gracefully(settings, monkeypatch):
    assert context.deepwiki_context(settings, "x/y")["status"] == "unavailable"  # offline
    assert context.codewiki_context(settings)["status"] == "not_imported"
    monkeypatch.setattr(context.shutil, "which", lambda *_: None)
    monkeypatch.setattr(context.sys, "executable", "/nonexistent/python")
    assert context.graphify_graph(settings, settings.repo, settings.work / "g")["status"] == "unavailable"


CAND = {"candidate_id": "RA-x", "dependency": "x", "categories": ["runtime-failure"], "files": [], "symbols": [],
        "target_tests": [], "score": 5,
        "evidence": [{"id": "E1", "kind": "source", "text": "declared", "source": "r.txt:1"},
                     {"id": "E2", "kind": "executed", "text": "pytest FAILED in py3.12/requirements", "source": "log"}]}


def _votes(verdict, cite=("E2",)):
    return {v: {"votes": [{"candidate_id": "RA-x", "verdict": verdict, "severity": "high", "rationale": "r",
                           "cited_evidence": list(cite), "counterevidence": [], "missing_evidence": []}]}
            for v in discovery.VOTERS}


def _decision(cls, cite=("E2",)):
    return {"candidate_id": "RA-x", "title": "t", "description": "d", "proposed_classification": cls,
            "severity": "high", "rationale": "r", "cited_evidence": list(cite), "uncertainties": [], "disagreements": []}


def test_supported_needs_valid_evidence():
    cls, notes = discovery.classify(dict(CAND, evidence=CAND["evidence"][:1], categories=["outdated"]),
                                    _votes("uncertain", ["E9"]), _decision("SUPPORTED", ["E9"]))
    assert cls == "NEEDS_INVESTIGATION"
    assert any("unknown evidence" in n for n in notes)


def test_runtime_claim_requires_executed_evidence():
    cand = dict(CAND, evidence=CAND["evidence"][:1])
    cls, _ = discovery.classify(cand, _votes("real_issue", ["E1"]), _decision("SUPPORTED", ["E1"]))
    assert cls == "NEEDS_INVESTIGATION"


def test_dismissal_overridden_by_executed_failure():
    cls, notes = discovery.classify(CAND, _votes("uncertain"), _decision("UNSUPPORTED"))
    assert cls == "NEEDS_INVESTIGATION" and any("contradicts" in n for n in notes)


def test_unanimous_rejection_without_execution_is_unsupported():
    cand = dict(CAND, evidence=CAND["evidence"][:1], categories=["outdated"])
    cls, _ = discovery.classify(cand, _votes("not_an_issue", ["E1"]), _decision("NEEDS_INVESTIGATION", ["E1"]))
    assert cls == "UNSUPPORTED"


def test_voters_are_independent_and_votes_persist(settings):
    seen = []

    def responder(role, prompt, schema):
        seen.append((role, prompt))
        if role == "adjudicator":
            return {"decisions": [_decision("SUPPORTED")]}
        return {"votes": [_votes("real_issue")["discovery"]["votes"][0]]}

    fake = FakeProvider(responder)
    votes, adj = discovery.run_council(fake, [CAND], settings.db(), "abc", log=lambda *_: None)
    voter_prompts = [p for r, p in seen if r != "adjudicator"]
    assert len(set(voter_prompts)) == 1  # identical snapshot, no other voter's output inside
    assert "verdict" not in voter_prompts[0]
    assert "real_issue" in [p for r, p in seen if r == "adjudicator"][0]  # adjudicator sees all votes
    issue = discovery.make_issue(CAND, votes, adj["decisions"][0], "abc")
    assert issue["classification"] == "SUPPORTED" and issue["approval_status"] == "pending"
    assert json.loads(json.dumps(issue))["discovery_votes"]["skeptic"]["verdict"] == "real_issue"
