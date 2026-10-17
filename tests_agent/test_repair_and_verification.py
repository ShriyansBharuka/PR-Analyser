import os
import sys

import pytest

from resilience_agent import repair, verification
from resilience_agent.storage import DB


def cand(cid, strategy, diff="x\n", valid=True, status="proposed"):
    return {"cand_id": cid, "strategy": strategy, "strategy_name": strategy, "diff": diff, "valid": valid,
            "status": status, "proposal": {"diagnosis": "", "solution": "s", "risks": []}, "changes": []}


def rev(reviewer, cid, score, verdict="accept", defects=()):
    s = {k: score for k in repair.CRITERIA}
    return {"reviewer": reviewer, "cand_id": cid, "scores": s, "verdict": verdict, "defects": list(defects)}


def test_anonymous_labels_hide_strategy():
    labels = repair.anonymize([cand("A1", "A"), cand("B1", "B"), cand("C1", "C")], seed="s")
    assert sorted(labels.values()) == ["A1", "B1", "C1"]
    assert not any(k in "".join(labels) for k in ("A1", "B1", "C1", "minimal", "migration"))


def test_reviewer_never_sees_own_patch():
    seen = {}

    class P:
        def complete(self, role, system, prompt, schema, temperature=0.2, tag=""):
            seen["prompt"] = prompt
            return schema.model_validate({"reviews": []}), {}
    cands = [cand("A1", "A", "DIFF-OF-A"), cand("B1", "B", "DIFF-OF-B")]
    issue = {"issue_id": "RA-x", "title": "t", "description": "", "affected_dependencies": ["x"], "evidence": []}
    repair.review(P(), issue, cands, "A", repair.anonymize(cands, "s"), {}, log=lambda *_: None)
    assert "DIFF-OF-B" in seen["prompt"] and "DIFF-OF-A" not in seen["prompt"]


def test_validator_requires_consistent_specs_across_manifests(tiny_repo):
    allowed = ["requirements.txt", "pyproject.toml"]
    one = {"edits": [{"path": "pyproject.toml", "find": '"oldlib>=2"', "replace": '"oldlib>=3"'}]}
    _, probs = repair.validate_changes(one, allowed, tiny_repo, scope=["oldlib"])
    assert any("manifests disagree" in p and "requirements.txt:1" in p for p in probs)
    both = {"edits": [{"path": "pyproject.toml", "find": '"oldlib>=2"', "replace": '"oldlib>=3"'},
                      {"path": "requirements.txt", "find": "oldlib==1.0", "replace": "oldlib>=3"}]}
    changes, probs = repair.validate_changes(both, allowed, tiny_repo, scope=["oldlib"])
    assert not probs and len(changes) == 2


def test_duplicate_of_failed_patch_is_excluded():
    cands = [cand("A1", "A", "same\n", status="failed"), cand("A2", "A", "same\n"), cand("C1", "C", "other\n")]
    assert repair.failed_duplicate(cands[1], cands) == "A1"
    assert [r["cand_id"] for r in repair.rank(cands, [], {})] == ["C1"]


def test_retry_feedback_has_gate_reasons_locations_and_defects():
    fb = repair.failure_feedback({"failed_tests": [], "failures": []}, {"regressions": []},
                                 reasons=["install paths still resolve different versions"],
                                 declarations={"x": [{"file": "requirements.txt", "line": 4, "name": "x", "spec": "==1"}]},
                                 defects=["only pyproject.toml is changed"])
    assert "GATE: install paths" in fb and "requirements.txt:4 declares x==1" in fb
    assert "REVIEWER DEFECT: only pyproject.toml is changed" in fb


def test_retry_changes_sampling_and_labels_failed_patch(tiny_repo):
    seen = []

    class P:
        def complete(self, role, system, prompt, schema, temperature=0.2, tag="", seed=None):
            seen.append((prompt, temperature, seed))
            return schema.model_validate({"diagnosis": "d", "solution": "s", "edits": [
                {"path": "requirements.txt", "find": "oldlib==1.0", "replace": "oldlib>=2"}]}), {}
    index = {"meta": {"path": str(tiny_repo)}, "scan": {"packages": [], "dep_files": ["requirements.txt"]},
             "usages": {"packages": {}}}
    issue = {"issue_id": "RA-x", "title": "t", "description": "", "affected_dependencies": ["zzz"], "evidence": []}
    repair.propose(P(), issue, index, {}, "A", log=lambda *_: None)
    repair.propose(P(), issue, index, {}, "A", feedback="f", prior={"diff": "OLD"}, prior_failed=True,
                   attempt=2, log=lambda *_: None)
    assert seen[0][2] is None and seen[1][2] is not None and seen[1][1] > seen[0][1]
    assert "THIS FAILED, do not repeat it" in seen[1][0] and "OLD" in seen[1][0]


def test_review_labels_are_normalised():
    shown = {"Proposal-P": 1, "Proposal-Q": 2}
    for raw in ("P", "Proposal-P", "proposal p", "### Proposal-P:", "p"):
        assert repair.normalize_label(raw, shown) == "Proposal-P", raw
    assert repair.normalize_label("R", shown) is None and repair.normalize_label("", shown) is None


def test_rank_is_deterministic_with_tiebreak():
    cands = [cand("A1", "A", "a\nb\nc\n"), cand("B1", "B", "a\n"), cand("C1", "C", valid=False)]
    reviews = [rev("C", "A1", 4), rev("C", "B1", 4)]
    table = repair.rank(cands, reviews, {})
    assert [r["cand_id"] for r in table] == ["B1", "A1"]  # tie -> smaller diff; invalid excluded


def test_weights_reward_opposition_only_when_verification_confirms():
    reviews = [rev("B", "A1", 5, "accept"), rev("C", "A1", 1, "reject")]
    w = repair.update_weights({}, cand("A1", "A"), reviews, passed=False)
    assert w["A"] < 1 and w["B"] < 1 and w["C"] > 1
    w2 = repair.update_weights({}, cand("A1", "A"), reviews, passed=True)
    assert w2["A"] > 1 and w2["B"] > 1 and w2["C"] < 1
    w3 = {"A": 0.2}
    assert repair.update_weights(w3, cand("A1", "A"), [], passed=False)["A"] == 0.2  # clipped


def test_changes_outside_allow_list_are_rejected(tiny_repo):
    p = {"edits": [{"path": "tests/test_core.py", "find": "1", "replace": "2"},
                   {"path": "../evil.py", "find": "x", "replace": "y"}]}
    changes, problems = repair.validate_changes(p, ["requirements.txt"], tiny_repo)
    assert not changes and len(problems) == 2


def test_edits_must_match_once_and_keep_files_parseable(tiny_repo):
    allowed = ["requirements.txt", "pyproject.toml", "demo/core.py"]
    ok, probs = repair.validate_changes({"edits": [{"path": "requirements.txt", "find": "oldlib==1.0",
                                                    "replace": "oldlib>=2"}]}, allowed, tiny_repo)
    assert not probs and ok[0]["content"] == "oldlib>=2\n"
    _, probs = repair.validate_changes({"edits": [{"path": "requirements.txt", "find": "nope", "replace": "x"}]},
                                       allowed, tiny_repo)
    assert "occurs 0 times" in probs[0]
    _, probs = repair.validate_changes({"edits": [{"path": "pyproject.toml", "find": "[project]", "replace": "[project"}]},
                                       allowed, tiny_repo)
    assert "does not parse" in probs[0]
    (tiny_repo / "requirements.txt").write_text("a\nb\nc\nd\ne\nf\n", encoding="utf-8")
    _, probs = repair.validate_changes({"edits": [{"path": "requirements.txt", "find": "b\nc\nd\ne\nf\n",
                                                   "replace": ""}]}, allowed, tiny_repo)
    assert "truncation guard" in probs[0]


def test_worktree_patch_leaves_primary_tree_untouched(settings, tiny_repo):
    from conftest import git
    (tiny_repo / "demo" / "cli.py").write_text("# uncommitted user work\n", encoding="utf-8")
    head = git(tiny_repo, "rev-parse", "HEAD")
    c = cand("A1", "A") | {"changes": [{"path": "requirements.txt", "content": "oldlib>=2\n"}]}
    applied = repair.apply_in_worktree(settings, {"issue_id": "RA-x"}, c, head)
    assert git(tiny_repo, "rev-parse", "HEAD") == head
    assert (tiny_repo / "requirements.txt").read_text() == "oldlib==1.0\n"
    assert (tiny_repo / "demo" / "cli.py").read_text() == "# uncommitted user work\n"
    assert "oldlib>=2" in applied["patch"] and applied["branch"] == "ra/RA-x-A1"
    verification.remove_worktree(settings, applied["worktree"], applied["branch"])
    assert "ra/RA-x-A1" not in git(tiny_repo, "branch")


def test_runner_restrictions(tmp_path, monkeypatch):
    r = verification.CommandRunner([tmp_path], tmp_path / "logs", timeout=5)
    with pytest.raises(PermissionError):
        r.run(["bash", "-c", "echo hi"], tmp_path, "x")
    with pytest.raises(PermissionError):
        r.run([sys.executable, "-c", "1"], tmp_path.parent, "x")
    monkeypatch.setenv("GITHUB_TOKEN", "secret-value")
    ok = r.run([sys.executable, "-c", "import os,sys; print(os.environ.get('GITHUB_TOKEN')); sys.exit(3)"], tmp_path, "env")
    assert ok["exit_code"] == 3 and not ok["ok"] and "secret-value" not in ok["stdout_tail"]
    slow = r.run([sys.executable, "-c", "import time; time.sleep(10)"], tmp_path, "slow", timeout=1)
    assert slow["timed_out"] and slow["exit_code"] is None and os.path.exists(slow["log"])


def _env(py, mode, status="passed", tests=None, checks=None, resolved=None):
    return {"python": py, "mode": mode, "status": status, "tests": tests or {}, "acceptance_tests": {},
            "checks": checks or {"pytest": {"ok": status == "passed", "exit_code": 0, "log": "l", "stderr_tail": "",
                                            "stdout_tail": ""}}, "resolved": resolved or {}}


def test_gate_and_comparison_block_regressions_and_missing_interpreters():
    base = {"matrix": [_env("3.12", "requirements", tests={"t::a": {"outcome": "passed"}})]}
    bad = {"matrix": [_env("3.12", "requirements", "failed", tests={"t::a": {"outcome": "failed"}}),
                      {"python": "3.13", "mode": "requirements", "status": "not_tested", "checks": {}}]}
    cmp = verification.compare(base, bad)
    assert "py3.12/requirements t::a" in cmp["regressions"]
    g = verification.gate(bad)
    assert not g["passed"] and g["missing_interpreters"] == ["3.13"]
    issue = {"affected_dependencies": ["x"], "scope": ["x"], "target_tests": []}
    ig = repair.issue_gate(bad, base, issue)
    assert not ig["passed"] and ig["new_test_failures"]


def test_issue_gate_requires_consistent_versions_across_install_paths():
    base = {"matrix": [_env("3.12", "requirements"), _env("3.12", "package")]}
    cand_ = {"matrix": [_env("3.12", "requirements", resolved={"x": "1.0"}), _env("3.12", "package", resolved={"x": "2.0"})]}
    g = repair.issue_gate(cand_, base, {"scope": ["x"], "affected_dependencies": ["x"], "target_tests": []})
    assert not g["passed"] and g["inconsistent"]


def test_persistence_survives_restart(tmp_path):
    db = DB(tmp_path / "a.sqlite")
    db.save_issue({"issue_id": "RA-x", "approval_status": "pending"})
    db.record_approval("RA-x", "issue", "approve", "ok")
    db.add_vote("discovery", "RA-x", "skeptic", {"verdict": "uncertain"})
    db.conn.close()
    db2 = DB(tmp_path / "a.sqlite")
    assert db2.latest_decision("RA-x", "issue") == "approve"
    assert db2.votes("RA-x")[0]["data"]["verdict"] == "uncertain"
