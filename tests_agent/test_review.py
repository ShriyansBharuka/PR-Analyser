"""Review mode building blocks: diff parsing, code graph, blast radius levels, test selection, verdict, intent review."""
from conftest import fixture_repo

from resilience_agent import review
from resilience_agent.llm import FakeProvider


def graphs(tmp_path, case):
    repo, base, head = fixture_repo(tmp_path, case)
    from conftest import git
    bwt, hwt = tmp_path / "b", tmp_path / "h"
    git(repo, "worktree", "add", "--detach", str(bwt), base)
    git(repo, "worktree", "add", "--detach", str(hwt), head)
    diff = review.parse_diff(review.diff_text(repo, base, head))
    return repo, bwt, hwt, diff, review.codegraph(bwt), review.codegraph(hwt)


def test_parse_diff_ranges():
    text = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -3,2 +3 @@ x\n-a\n-b\n+c\n"
            "diff --git a/n.py b/n.py\n--- /dev/null\n+++ b/n.py\n@@ -0,0 +1,2 @@\n+x\n+y\n")
    d = review.parse_diff(text)
    assert d["x.py"]["old"] == [(3, 4)] and d["x.py"]["new"] == [(3, 3)] and d["x.py"]["status"] == "modified"
    assert d["n.py"]["status"] == "added" and d["n.py"]["old"] == [] and d["n.py"]["new"] == [(1, 2)]


def test_rename_maps_to_symbols_callers_tests_and_entry_point(tmp_path):
    repo, bwt, hwt, diff, bg, hg = graphs(tmp_path, "renamed_function")
    empty = {"status": "unavailable"}
    blast = review.blast_radius(diff, bg, hg, empty, empty, review.entrypoints(bwt), review.entrypoints(hwt))
    changed = {c["symbol"]: c["change"] for c in blast["changed"]}
    assert changed["pkg.core.compute_total"] == "removed" and changed["pkg.core.calculate_total"] == "added"
    assert "pkg.cli.main" in changed  # its body now calls the new name
    assert [t["test"] for t in blast["affected_tests"]] == ["tests/test_cli.py::test_main"]
    assert blast["affected_tests"][0]["uncertain"]  # only the static source found it (no coverage run here)
    assert [e["name"] for e in blast["entry_points"]] == ["pkgcli"]
    assert all(c["label"] == "OBSERVED" for c in blast["changed"])


def test_dynamic_coverage_confirms_static_tests(tmp_path):
    repo, bwt, hwt, diff, bg, hg = graphs(tmp_path, "changed_default")
    cov = {"status": "ok", "lines": {"pkg/text.py": {1: ["tests/test_text.py::test_greet"],
                                                      2: ["tests/test_text.py::test_greet"]}}}
    blast = review.blast_radius(diff, bg, hg, cov, cov, {}, {})
    t = blast["affected_tests"][0]
    assert t["test"] == "tests/test_text.py::test_greet" and t["sources"] == ["dynamic", "static"] and not t["uncertain"]
    assert t["label"] == "VERIFIED"
    assert blast["coverage"] == {"label": "VERIFIED", "available": True, "affected_functions": 1, "reached": 1,
                                 "percent": 100.0, "unreached": []}


def test_selection_runs_affected_files_or_full_suite_on_config_change(tmp_path):
    repo, bwt, hwt, diff, bg, hg = graphs(tmp_path, "changed_default")
    blast = review.blast_radius(diff, bg, hg, {}, {}, {}, {})
    assert review.select_tests(blast, bwt, hwt) == (["tests/test_text.py"], ["tests/test_text.py"])
    assert review.select_tests(blast, bwt, hwt, full=True) == (None, None)
    blast["config_files_changed"] = ["pyproject.toml"]
    assert review.select_tests(blast, bwt, hwt) == (None, None)


def _run(status="passed", tests=None):
    return {"logs": "logs", "matrix": [{"python": "3.12", "mode": "requirements", "status": status, "checks": {},
                                        "tests": tests or {}}]}


def _blast(changed=True, tests=True):
    return {"changed": [{"symbol": "m.f", "test": False}] if changed else [],
            "affected_tests": [{"test": "t"}] if tests else [],
            "coverage": {"available": True, "reached": 1 if tests else 0}, "dynamic": {"head": {"log": "cov.log"}}}


NO_STRUCT = {"ref": "s.json", "api": {"breaking": []}, "cycles": {"new": []}, "layers": {"new": []},
             "packaging": {"new_undeclared_imports": [], "entry_points": {"removed": []}}}


def test_verdict_precedence():
    ok = {"regressions": [], "fixed": []}
    assert review.verdict(_run("not_tested"), _run(), ok, _blast(), NO_STRUCT, {})[0] == "UNVERIFIABLE"
    assert review.verdict(_run(), _run(), {"regressions": ["py3.12 t"], "fixed": []}, _blast(), NO_STRUCT, {})[0] \
        == "REGRESSION_FOUND"
    assert review.verdict(_run(), _run(), ok, _blast(tests=False), NO_STRUCT, {})[0] == "UNVERIFIABLE"
    broken = NO_STRUCT | {"api": {"breaking": [{"explanation": "m.f removed"}]}}
    v, reasons = review.verdict(_run(), _run(), ok, _blast(), broken, {})
    assert v == "NEEDS_HUMAN" and reasons[0]["label"] == "OBSERVED"
    beh = {"diffs": [{"function": "m:f", "input": "1", "base": "return 1", "head": "return 2", "ref": "r"}]}
    assert review.verdict(_run(), _run(), ok, _blast(), NO_STRUCT, beh)[0] == "NEEDS_HUMAN"
    assert review.verdict(_run(), _run(), {"regressions": [], "fixed": ["x"]}, _blast(), NO_STRUCT, {})[0] == "FIXED"
    assert review.verdict(_run(), _run(), ok, _blast(), NO_STRUCT, {})[0] == "SAFE"


def test_intent_review_drops_mismatches_that_cite_no_changed_file():
    def responder(role, prompt, schema):
        return {"stated_intent": "docs only", "summary": "changes code", "mismatches": [
            {"claim": "docs only", "observed": "changes greet", "files": ["pkg/text.py"], "severity": "high"},
            {"claim": "x", "observed": "invented", "files": ["nowhere.py"], "severity": "low"}]}
    blast = {"changed": [{"symbol": "pkg.text.greet", "change": "modified"}], "files": ["pkg/text.py"]}
    out = review.intent_review(FakeProvider(responder), "Docs only", "diff", blast, {"api": {"breaking": []}},
                               log=lambda *_: None)
    assert out["label"] == "AI" and [m["files"] for m in out["mismatches"]] == [["pkg/text.py"]]
    assert out["dropped"][0]["rule"] == "cites no changed file"


def test_decorator_registers_command_without_making_it_a_caller(tmp_path):
    (tmp_path / "app.py").write_text(
        "def group(f):\n    return f\n\n@group\ndef cli():\n    pass\n\n"
        "@cli.command()\ndef report():\n    pass\n\n@cli.command()\ndef info():\n    pass\n")
    g = review.codegraph(tmp_path)
    assert {"app.report", "app.info"} <= g["calls"]["app.cli"]  # registration: cli reaches its commands
    assert "app.cli" not in g["calls"]["app.report"] | g["calls"]["app.info"]
    assert review.reverse_walk([g], {"app.report"}) == {"app.cli": 1, "app.group": 2}  # @group wraps cli


def test_def_line_only_change_is_matched_per_function(tmp_path):
    repo, bwt, hwt, diff, bg, hg = graphs(tmp_path, "renamed_function")
    # coverage marks the def line as import-time (no test context); only the body lines carry the test
    cov = {"status": "ok", "lines": {"pkg/core.py": {2: ["tests/test_cli.py::test_main"]}}}
    blast = review.blast_radius(diff, bg, hg, cov, cov, {}, {})
    assert blast["affected_tests"][0]["sources"] == ["dynamic", "static"]
