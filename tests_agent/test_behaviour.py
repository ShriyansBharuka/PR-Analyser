"""Purity filter and generated-test validation (no venv needed)."""
from conftest import fixture_repo

from resilience_agent import behaviour, review


def test_pure_functions_exclude_io_clock_and_impure_callees(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "m.py").write_text(
        "import os\nfrom datetime import date\n\n"
        "def pure(x):\n    return x * 2\n\n"
        "def reads():\n    return open('f').read()\n\n"
        "def env():\n    return os.environ.get('X')\n\n"
        "def today():\n    return date.today()\n\n"
        "def calls_impure():\n    return reads()\n\n"
        "def calls_pure(y):\n    return pure(y) + 1\n\n"
        "def gen():\n    yield 1\n")
    pure = behaviour.pure_functions(review.codegraph(tmp_path), tmp_path)
    assert pure == {"pkg.m.pure", "pkg.m.calls_pure"}


def test_fixture_function_is_pure(tmp_path):
    repo, _, _ = fixture_repo(tmp_path, "behaviour_change")
    assert "pkg.norm.normalise" in behaviour.pure_functions(review.codegraph(repo), repo)


def test_generated_tests_are_validated():
    assert behaviour._validate_generated("def test_x():\n    assert 1\n") is None
    assert "forbidden" in behaviour._validate_generated("import os\n\ndef test_x():\n    os.remove('a')\n")
    assert "no test" in behaviour._validate_generated("x = 1\n")
    assert "parse" in behaviour._validate_generated("def (:\n")
