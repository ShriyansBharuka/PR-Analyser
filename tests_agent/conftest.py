"""Fixtures for the agent's own tests. Everything here uses the FAKE provider: these tests prove the
plumbing and the rules, never that real model reasoning works (see tests_agent/test_live_model.py)."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURES = Path(__file__).resolve().parent / "fixtures"
collect_ignore_glob = ["fixtures/*"]  # fixture repos contain their own test_*.py files

FILES = {
    "requirements.txt": "oldlib==1.0\n",
    "requirements-dev.txt": "pytest\n",
    "pyproject.toml": '[project]\nname = "demo"\nversion = "0.1"\nrequires-python = ">=3.10"\n'
                      'dependencies = ["oldlib>=2"]\n[project.scripts]\ndemo = "demo.cli:main"\n'
                      '[tool.setuptools]\npackages = ["demo"]\n',
    "demo/__init__.py": "",
    "demo/core.py": "import oldlib\nfrom oldlib.utils import _hidden\n\ndef value():\n    return 1\n",
    "demo/cli.py": "def main():\n    print('demo')\n",
    "tests/test_core.py": "def test_ok():\n    assert 1 + 1 == 2\n",
}


def git(repo, *args):
    # hooks off: a git template hook (e.g. graphify's post-commit rebuild) would race with the next commit
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                           "-c", f"core.hooksPath={ROOT / '.no-git-hooks'}", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def tiny_repo(tmp_path):
    repo = tmp_path / "repo"
    for name, text in FILES.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    git(repo.parent, "init", "-q", str(repo))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def fixture_repo(tmp_path, case):
    """tests_agent/fixtures/<case>/base is the first commit; head/ is overlaid as the second. -> (repo, base, head)."""
    import shutil
    repo = tmp_path / case
    shutil.copytree(FIXTURES / case / "base", repo)
    git(tmp_path, "init", "-q", str(repo))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    # copyfile, not copy2: a same-size file with the fixture's old mtime would look unchanged to git's stat cache
    shutil.copytree(FIXTURES / case / "head", repo, dirs_exist_ok=True, copy_function=shutil.copyfile)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "head")
    return repo, base, git(repo, "rev-parse", "HEAD")


def review_settings(repo):
    from resilience_agent.config import Settings
    s = Settings()
    s.repo, s.offline, s.pythons, s.install_modes, s.exclude = repo, True, ["3.12"], ["requirements"], []
    return s


@pytest.fixture
def settings(tiny_repo):
    from resilience_agent.config import Settings
    s = Settings()
    s.repo = tiny_repo
    s.offline = True
    s.pythons = ["3.12"]
    s.install_modes = ["requirements"]
    s.exclude = []
    return s
