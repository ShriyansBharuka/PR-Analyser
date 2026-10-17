"""Objective verifier. Runs real commands; never looks at agent confidence.

Isolation: each candidate runs in its own git worktree and its own virtualenv. This is the
REDUCED-TRUST LOCAL MODE: a worktree is not a security sandbox. Commands are restricted to an
allow-list, run without a shell, inside permitted roots, with timeouts and a scrubbed environment.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .config import ACCEPTANCE_DIR, Settings
from .storage import now

ALLOWED = {"python", "uv", "git", "graphify"}
SECRET_ENV = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|AUTH|COOKIE|SESSION", re.I)
SECRET_TEXT = re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}"
                         r"|xox[abprs]-[\w-]{10,}|(?i:bearer)\s+[\w.~+/=-]{16,}"
                         r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")
# narrower than SECRET_ENV on purpose: session/auth *identifiers* appear in paths and are not secrets
SECRET_TEXT_ENV = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|COOKIE|PRIVATE_?KEY", re.I)
SECRET_ASSIGN = re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)(\s*[=:]\s*)([^\s\"',]+)")


def scrub(text):
    """Redact secrets from text before it is persisted: values of secret-named env vars, then known token shapes."""
    for k, v in os.environ.items():
        if SECRET_TEXT_ENV.search(k) and len(v) >= 12:  # short values are not secrets
            text = text.replace(v, "[REDACTED]")
    return SECRET_ASSIGN.sub(r"\1\2[REDACTED]", SECRET_TEXT.sub("[REDACTED]", text))


class CommandRunner:
    def __init__(self, roots, log_dir: Path, timeout=600):
        self.roots = [Path(r).resolve() for r in roots]
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.count = 0

    def check(self, argv, cwd):
        exe = Path(argv[0]).name.lower().removesuffix(".exe")
        if not (exe in ALLOWED or re.fullmatch(r"python[\d.]*", exe)):
            raise PermissionError(f"command not allowed: {exe}")
        cwd = Path(cwd).resolve()
        if not any(cwd == r or r in cwd.parents for r in self.roots):
            raise PermissionError(f"cwd outside permitted workspace: {cwd}")
        return cwd

    def run(self, argv, cwd, label, timeout=None, env=None):
        cwd = self.check(argv, cwd)
        clean = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
        clean.update(env or {})
        clean["PYTHONIOENCODING"] = "utf-8"
        self.count += 1
        log = self.log_dir / f"{self.count:03d}-{re.sub(r'[^A-Za-z0-9_.-]+', '_', label)}.log"
        started = time.time()
        timed_out = False
        try:
            p = subprocess.run([str(a) for a in argv], cwd=cwd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout or self.timeout, env=clean)
            code, out, err = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired as e:
            timed_out, code = True, None
            out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            err = f"TIMEOUT after {timeout or self.timeout}s"
        except FileNotFoundError as e:
            code, out, err = None, "", f"executable not found: {e}"
        seconds = round(time.time() - started, 1)
        log.write_text(f"$ {' '.join(map(str, argv))}\n# cwd={cwd}\n# exit={code} timed_out={timed_out} "
                       f"seconds={seconds}\n\n--- stdout ---\n{out}\n--- stderr ---\n{err}\n", encoding="utf-8")
        return {"label": label, "argv": [str(a) for a in argv], "cwd": str(cwd), "exit_code": code,
                "timed_out": timed_out, "seconds": seconds, "ok": code == 0,
                "stdout_tail": out[-2500:], "stderr_tail": err[-2500:], "log": str(log)}


def find_python(version):
    uv = shutil.which("uv")
    if uv:
        p = subprocess.run([uv, "python", "find", version], capture_output=True, text=True)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    exe = shutil.which(f"python{version}")
    return exe


def venv_python(venv: Path):
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def manifest_hash(src: Path, version, mode):
    h = hashlib.sha1(f"{version}|{mode}".encode())
    for name in sorted(p.name for p in src.glob("requirements*.txt")) + ["pyproject.toml"]:
        if (src / name).exists() and name != "requirements-agent.txt":
            h.update(name.encode() + (src / name).read_bytes())
    return h.hexdigest()[:12]


def build_env(runner, src: Path, version, mode, venv_root: Path):
    """Create (or reuse) a venv for `src` installed via `mode`: requirements | package."""
    interp = find_python(version)
    if not interp:
        return None, [{"label": f"find-python-{version}", "ok": False, "exit_code": None,
                       "stderr_tail": f"Python {version} interpreter not available", "missing_interpreter": True}]
    venv = venv_root / f"py{version}-{mode}-{manifest_hash(src, version, mode)}"
    marker = venv / ".ra-installed.json"
    if marker.exists():
        return venv_python(venv), json.loads(marker.read_text())
    if venv.exists():
        shutil.rmtree(venv, ignore_errors=True)
    uv = shutil.which("uv")
    steps = []
    if uv:
        steps.append(runner.run([uv, "venv", "--python", interp, str(venv), "-q"], src, f"venv-{version}-{mode}"))
    else:
        steps.append(runner.run([interp, "-m", "venv", str(venv)], src, f"venv-{version}-{mode}"))
    py = venv_python(venv)
    dev = [x for f in sorted(src.glob("requirements-dev*.txt")) for x in ("-r", f.name)]
    if mode == "requirements":
        reqs = [x for f in sorted(src.glob("requirements*.txt"))
                if f.name != "requirements-agent.txt" and not f.name.startswith("requirements-dev")
                for x in ("-r", f.name)] + dev
        target = reqs
    else:
        target = ["."] + dev
    if uv:
        cmd = [uv, "pip", "install", "--python", str(py), *target]
    else:
        cmd = [py, "-m", "pip", "install", "-q", *target]
    steps.append(runner.run(cmd, src, f"install-{version}-{mode}"))
    steps[-1]["check"] = "install"
    if all(s["ok"] for s in steps):
        marker.write_text(json.dumps(steps))
    return py, steps


def parse_junit(path: Path):
    if not path.exists():
        return {}
    tests = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        name = f"{case.get('classname')}::{case.get('name')}"
        outcome = "passed"
        for child in case:
            if child.tag in ("failure", "error"):
                outcome = "failed" if child.tag == "failure" else "error"
                tests[name] = {"outcome": outcome, "message": (child.get("message") or "")[:400]}
                break
            if child.tag == "skipped":
                outcome = "skipped"
        tests.setdefault(name, {"outcome": outcome})
    return tests


def deprecation_warnings(text):
    """Unique warnings with file:line, including '* ...' continuation lines of multi-line messages."""
    found = []
    lines = text.splitlines()
    pat = re.compile(r"([^\s]+\.py):(\d+): (\w*DeprecationWarning|PydanticDeprecatedSince\w*|UserWarning|FutureWarning): (.+)")
    for i, line in enumerate(lines):
        m = pat.search(line)
        if not m:
            continue
        extra = []
        for nxt in lines[i + 1:i + 6]:
            if nxt.strip().startswith("* "):
                extra.append(nxt.strip())
            elif extra or not nxt.strip():
                break
        item = {"file": m.group(1).replace("\\", "/"), "line": int(m.group(2)), "category": m.group(3),
                "message": (m.group(4) + " " + " ".join(extra)).strip()[:400]}
        if item not in found:
            found.append(item)
    return found


def refresh_project(runner, py, src, version, mode):
    """Reinstall the project itself (no deps) so a reused package-mode venv tests THIS tree, not the one that built it."""
    uv = shutil.which("uv")
    if mode != "package":
        return None
    cmd = [uv, "pip", "install", "--python", str(py), "--no-deps", "--reinstall", "."] if uv else \
        [py, "-m", "pip", "install", "-q", "--no-deps", "--force-reinstall", "."]
    return runner.run(cmd, src, f"reinstall-{version}-{mode}")


def run_suite(settings: Settings, src: Path, label: str, package_modules, entrypoints, log=print,
              venv_root=None, reinstall_project=False, pytest_args=()):
    """Full objective check matrix for one source tree. Returns structured, comparable evidence.

    Review mode passes its own venv_root, reinstall_project=True and a test selection in pytest_args;
    the defaults keep maintenance mode exactly as it was."""
    src = Path(src).resolve()
    out_dir = settings.dir(f"artifacts/verification/{label}")
    runner = CommandRunner([settings.repo, src, settings.work], out_dir / "logs", settings.command_timeout)
    venv_root = venv_root or settings.dir("venvs")
    acceptance = ACCEPTANCE_DIR / src.name if (ACCEPTANCE_DIR / src.name).exists() else None
    if acceptance is None:
        candidates = [p for p in ACCEPTANCE_DIR.glob("*") if p.is_dir() and (src / p.name).exists()]
        acceptance = candidates[0] if candidates else None
    matrix = []
    for version in settings.pythons:
        for mode in settings.install_modes:
            log(f"[verify:{label}] python {version} / install via {mode}")
            env = {"python": version, "mode": mode, "checks": {}, "tests": {}, "warnings": []}
            py, steps = build_env(runner, src, version, mode, venv_root)
            if py is None:
                env["status"] = "not_tested"
                env["reason"] = steps[0]["stderr_tail"]
                matrix.append(env)
                log(f"[verify:{label}]   NOT TESTED: {env['reason']}")
                continue
            install = steps[-1]
            env["checks"]["install"] = _brief(install)
            if not install["ok"]:
                env["status"] = "failed"
                matrix.append(env)
                log(f"[verify:{label}]   install FAILED")
                continue
            if reinstall_project:
                r = refresh_project(runner, py, src, version, mode)
                if r:
                    env["checks"]["install"] = _brief(r)
                    if not r["ok"]:
                        env["status"] = "failed"
                        matrix.append(env)
                        log(f"[verify:{label}]   project reinstall FAILED")
                        continue
            uv = shutil.which("uv")
            if uv:
                env["checks"]["dependency_conflicts"] = _brief(runner.run([uv, "pip", "check", "--python", str(py)], src, f"pipcheck-{version}-{mode}"))
                freeze = runner.run([uv, "pip", "freeze", "--python", str(py)], src, f"freeze-{version}-{mode}")
                env["resolved"] = dict(l.split("==", 1) for l in freeze["stdout_tail"].splitlines() if "==" in l)
            imports = "import importlib, warnings; warnings.simplefilter('default'); " + "; ".join(
                f"importlib.import_module({m!r})" for m in package_modules)
            env["checks"]["import_smoke"] = _brief(runner.run([py, "-W", "default", "-c", imports], src, f"import-{version}-{mode}"))
            for name, target in entrypoints.items():
                mod, _, attr = target.partition(":")
                code = f"import sys; sys.argv=[{name!r}, '--help']; from {mod} import {attr} as f; f()"
                env["checks"][f"cli_help:{name}"] = _brief(runner.run([py, "-c", code], src, f"cli-{version}-{mode}"))
            junit = out_dir / f"junit-{version}-{mode}.xml"
            t = runner.run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-W", "default",
                            f"--junitxml={junit}", *pytest_args], src, f"pytest-{version}-{mode}")
            env["checks"]["pytest"] = _brief(t)
            env["tests"] = parse_junit(junit)
            env["warnings"] = deprecation_warnings(t["stdout_tail"] + env["checks"]["import_smoke"]["stderr_tail"])
            if acceptance:
                ajunit = out_dir / f"acceptance-{version}-{mode}.xml"
                a = runner.run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--rootdir={acceptance}",
                                f"--junitxml={ajunit}", str(acceptance)], src, f"acceptance-{version}-{mode}",
                               env={"RA_TARGET_SRC": str(src)})
                env["checks"]["acceptance"] = _brief(a)
                env["acceptance_tests"] = parse_junit(ajunit)
            env["status"] = "passed" if all(c["ok"] for c in env["checks"].values()) else "failed"
            log(f"[verify:{label}]   {env['status']}: " + ", ".join(
                f"{k}={'ok' if c['ok'] else 'FAIL'}" for k, c in env["checks"].items()))
            matrix.append(env)
    result = {"label": label, "src": str(src), "commit": _git_head(src), "ran_at": now(),
              "isolation": "reduced-trust local mode (git worktree + per-run virtualenv, allow-listed commands)",
              "acceptance_suite": str(acceptance) if acceptance else None, "matrix": matrix,
              "logs": str(out_dir / "logs")}
    (out_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def _brief(r):
    return {k: r.get(k) for k in ("label", "argv", "exit_code", "ok", "timed_out", "seconds",
                                   "stdout_tail", "stderr_tail", "log")}


def _git_head(src):
    p = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def gate(result, required_pythons=("3.12",)):
    """Mandatory acceptance gate. Every tested environment must pass every check; required Pythons must be tested."""
    tested = [e for e in result["matrix"] if e["status"] != "not_tested"]
    failures = [{"python": e["python"], "mode": e["mode"], "check": k, "exit_code": c["exit_code"],
                 "log": c["log"], "stderr_tail": c["stderr_tail"][-1200:], "stdout_tail": c["stdout_tail"][-1200:]}
                for e in tested for k, c in e["checks"].items() if not c["ok"]]
    failed_tests = [{"python": e["python"], "mode": e["mode"], "test": n, **t}
                    for e in tested for n, t in {**e.get("tests", {}), **e.get("acceptance_tests", {})}.items()
                    if t["outcome"] in ("failed", "error")]
    missing = sorted({e["python"] for e in result["matrix"] if e["status"] == "not_tested"})
    untested_required = [p for p in required_pythons if p not in {e["python"] for e in tested}]
    passed = bool(tested) and not failures and not untested_required
    return {"passed": passed, "failures": failures, "failed_tests": failed_tests,
            "missing_interpreters": missing, "untested_required": untested_required,
            "environments_tested": [f"py{e['python']}/{e['mode']}" for e in tested]}


def compare(baseline, candidate):
    """Before-versus-after: per environment, per check and per test."""
    def key(e):
        return f"py{e['python']}/{e['mode']}"
    base = {key(e): e for e in baseline["matrix"]}
    rows, regressions, fixed = [], [], []
    for e in candidate["matrix"]:
        b = base.get(key(e))
        for check in sorted(set(e.get("checks", {})) | set((b or {}).get("checks", {}))):
            before = (b or {}).get("checks", {}).get(check, {}).get("ok")
            after = e.get("checks", {}).get(check, {}).get("ok")
            rows.append({"env": key(e), "item": check, "before": before, "after": after})
            if before and after is False:
                regressions.append(f"{key(e)} {check}")
            if before is False and after:
                fixed.append(f"{key(e)} {check}")
        btests = {**(b or {}).get("tests", {}), **(b or {}).get("acceptance_tests", {})}
        for name, t in {**e.get("tests", {}), **e.get("acceptance_tests", {})}.items():
            before = btests.get(name, {}).get("outcome")
            if before == "passed" and t["outcome"] in ("failed", "error"):
                regressions.append(f"{key(e)} {name}")
            if before in ("failed", "error") and t["outcome"] == "passed":
                fixed.append(f"{key(e)} {name}")
        rows.append({"env": key(e), "item": "status", "before": (b or {}).get("status"), "after": e["status"]})
    return {"rows": rows, "regressions": regressions, "fixed": fixed}


# ---------------------------------------------------------------- isolated worktrees

def make_worktree(settings: Settings, name, commit, branch=None):
    """Create .resilience/worktrees/<name> at `commit`. Never touches the primary working tree."""
    path = settings.dir("worktrees") / name
    if path.exists():
        return path
    args = ["git", "-C", str(settings.repo), "worktree", "add"]
    args += ["-b", branch, str(path), commit] if branch else ["--detach", str(path), commit]
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {p.stderr.strip()}")
    return path


def remove_worktree(settings: Settings, path, branch=None):
    subprocess.run(["git", "-C", str(settings.repo), "worktree", "remove", "--force", str(path)],
                   capture_output=True, text=True)
    if branch:
        subprocess.run(["git", "-C", str(settings.repo), "branch", "-D", branch], capture_output=True, text=True)


def baseline(settings: Settings, db, index_result, log=print, refresh=False):
    """Run the full check matrix on the committed HEAD snapshot (not the dirty working tree)."""
    commit = index_result["meta"]["commit"]
    cached = db.get(f"baseline:{commit}")
    if cached and not refresh:
        log(f"[baseline] reusing stored baseline for {commit[:12]}")
        return cached
    wt = make_worktree(settings, f"baseline-{commit[:12]}", commit)
    mods = package_modules(index_result)
    result = run_suite(settings, wt, f"baseline-{commit[:12]}", mods, index_result["entrypoints"], log)
    result["gate"] = gate(result)
    db.put(f"baseline:{commit}", result)
    db.put("baseline:latest", result)
    return result


def package_modules(index_result):
    """Importable non-test modules of the target project."""
    return sorted(m["module"] for m in index_result["modules"]
                  if m.get("module") and not m["is_test"] and not m["file"].startswith(("tests/", "test/"))
                  and m["file"] not in ("setup.py", "conftest.py"))
