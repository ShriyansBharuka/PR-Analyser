#!/usr/bin/env python3
"""Offline self-check for scan_deps.py and find_usages.py on a tiny fake repo.

Usage: python selftest.py      (prints OK, or fails with an AssertionError)
"""
import tempfile
from pathlib import Path

from find_usages import find_usages
from scan_deps import scan

FILES = {
    "requirements.txt": "oldlib==1.0\nunusedlib\n# ghostlib==2.0\n# just a note\n-r requirements-dev.txt\n",
    "requirements-dev.txt": "pytest\n",
    "pyproject.toml": '[project]\nname = "demo"\nrequires-python = ">=3.10"\n'
                      'dependencies = ["oldlib>=2", "python-fancy"]\n'
                      '[build-system]\nrequires = ["setuptools>=61"]\n',
    "Dockerfile": "FROM python:3.12-slim\n",
    "Makefile": "test:\n\tpytest\n",
    "app/__init__.py": "",
    "app/core.py": "import oldlib\nfrom oldlib.utils import _secret\nfrom fancy import thing\n"
                   "import undeclaredlib\nfrom . import helpers\n"
                   "oldlib.Client.send = lambda *a: None\noldlib.tools.run()\n",
    "app/helpers.py": "import os\n",
    "tests/test_core.py": "from app import core\n",
}


def main():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        for name, text in FILES.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(text, encoding="utf-8")

        s = scan(repo, offline=True)
        pkgs = {p["name"]: p for p in s["packages"]}
        assert s["dep_files"] == ["requirements-dev.txt", "requirements.txt", "pyproject.toml"], s["dep_files"]
        assert s["target_python"] == "3.12" and s["target_python_source"] == "Dockerfile"
        assert [c["text"] for c in s["commented_out"]] == ["# ghostlib==2.0"]
        assert pkgs["oldlib"]["pinned_version"] == "1.0"
        assert pkgs["oldlib"]["risk_flags"]["specs_differ"] and pkgs["oldlib"]["risk_flags"]["exact_pin"]
        assert pkgs["unusedlib"]["risk_flags"]["unpinned"]
        assert not pkgs["setuptools"]["risk_flags"]["specs_differ"]  # build-system only
        assert pkgs["oldlib"]["pypi"] is None  # offline

        u = find_usages(repo)
        old = u["packages"]["oldlib"]
        assert [p["symbol"] for p in old["private_api"]] == ["oldlib.utils._secret"]
        assert [m["target"] for m in old["monkeypatches"]] == ["oldlib.Client.send"]
        assert "oldlib.tools.run" in old["api_symbols"]
        assert old["covering_tests"] == ["tests/test_core.py"]
        assert u["packages"]["python-fancy"]["match_method"] == "prefix-match"
        assert "undeclaredlib" in u["unmapped_imports"]
        assert "unusedlib" in u["declared_unused"]
        assert "pytest" not in u["declared_unused"]  # mentioned in Makefile
        assert u["packages"]["pytest"]["text_mentions"][0]["file"] == "Makefile"
        assert "os" not in u["packages"] and "app" not in u["packages"]  # stdlib / local skipped
    print("OK")


if __name__ == "__main__":
    main()
