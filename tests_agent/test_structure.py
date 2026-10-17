"""Structural checks on fixture repos with known changes. Everything here is deterministic and OBSERVED."""
from conftest import fixture_repo, git, review_settings

from resilience_agent import review, structure


def run_check(tmp_path, case):
    repo, base, head = fixture_repo(tmp_path, case)
    bwt, hwt = tmp_path / "b", tmp_path / "h"
    git(repo, "worktree", "add", "--detach", str(bwt), base)
    git(repo, "worktree", "add", "--detach", str(hwt), head)
    facts = {"base": review.tree_facts(bwt, []), "head": review.tree_facts(hwt, [])}
    diff = review.parse_diff(review.diff_text(repo, base, head))
    return structure.check(review_settings(repo), bwt, hwt, review.codegraph(bwt), review.codegraph(hwt), facts, diff,
                           tmp_path / "structure.json")


def test_renamed_public_function_is_removed_and_paired(tmp_path):
    res = run_check(tmp_path, "renamed_function")
    removed = [b for b in res["api"]["breaking"] if b["symbol"] == "pkg.core.compute_total"]
    assert removed and removed[0]["kind"] == "Public object was removed" and removed[0]["label"] == "OBSERVED"
    assert removed[0]["likely_rename_to"] == "pkg.core.calculate_total"
    assert (tmp_path / "structure.json").exists()


def test_api_diff_reads_the_worktrees_not_a_same_named_package_on_sys_path(tmp_path, monkeypatch):
    shadow = tmp_path / "shadow" / "pkg"  # another copy of `pkg` that a name lookup would find first
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("")
    (shadow / "core.py").write_text("def compute_total(items):\n    return 0\n")
    monkeypatch.syspath_prepend(str(tmp_path / "shadow"))
    res = run_check(tmp_path, "renamed_function")
    assert any(b["symbol"] == "pkg.core.compute_total" for b in res["api"]["breaking"])


def test_changed_default_is_reported(tmp_path):
    res = run_check(tmp_path, "changed_default")
    kinds = {b["kind"]: b for b in res["api"]["breaking"]}
    assert "Parameter default was changed" in kinds
    assert "'!' -> '?'" in kinds["Parameter default was changed"]["explanation"]


def test_new_import_cycle_and_layer_rule(tmp_path):
    res = run_check(tmp_path, "import_cycle")
    assert res["cycles"]["base"] == [] and res["cycles"]["new"] == [["pkg.a", "pkg.b"]]
    assert res["layers"]["new"] == [{"rule": "pkg.b must not import pkg.a", "chain": ["pkg.b", "pkg.a"]}]


def test_cycles_and_layers_on_plain_graphs():
    imports = {"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": {"a"}}
    assert structure.cycles(imports) == [["a", "b", "c"]]
    v = structure.layer_violations(imports, [{"source": "d", "target": "c"}])
    assert v == [{"rule": "d must not import c", "chain": ["d", "a", "b", "c"]}]


def test_packaging_reports_removed_entry_point(tmp_path):
    repo, base, head = fixture_repo(tmp_path, "renamed_function")
    (repo / "pyproject.toml").write_text('[project]\nname = "pkg"\nversion = "0.1"\ndependencies = ["requests"]\n',
                                         encoding="utf-8")
    git(repo, "commit", "-qam", "drop script")
    bwt, hwt = tmp_path / "b", tmp_path / "h"
    git(repo, "worktree", "add", "--detach", str(bwt), base)
    git(repo, "worktree", "add", "--detach", str(hwt), "HEAD")
    facts = {"base": review.tree_facts(bwt, []), "head": review.tree_facts(hwt, [])}
    pk = structure.packaging(bwt, hwt, facts, [])
    assert pk["entry_points"]["removed"] == ["pkgcli = pkg.cli:main"]
    assert pk["dependencies"]["added"] == ["requests"]
