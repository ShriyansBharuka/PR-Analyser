#!/usr/bin/env python3
"""List a repo's declared Python dependencies and fetch facts about each from PyPI.

Everything in the output is OBSERVED (read from files or PyPI), never guessed.
Standard library only, Python 3.11+.

Usage:
    python scan_deps.py --repo PATH [--out FILE] [--offline] [--stale-days 730] [--target-python 3.12]
"""
import argparse
import json
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

# name, [extras], version spec, ; marker   e.g.  requests[socks]>=2.0 ; python_version<"3.12"
REQ_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*([^;#]*?)\s*(?:;\s*([^#]*?))?\s*(?:\s#.*)?$"
)
SPEC_START = ("<", ">", "=", "!", "~", "@", "(")
PYPI = "https://pypi.org"


def normalize(name):
    """PEP 503 name normalization: 'Jinja2' -> 'jinja2', 'python_dateutil' -> 'python-dateutil'."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirement(text):
    """Turn 'Jinja2==2.11.3' into a dict, or return None if it is not a requirement."""
    m = REQ_RE.match(text.strip())
    if not m:
        return None
    name, extras, spec, marker = m.groups()
    spec = spec.replace(" ", "")
    if spec and not spec.startswith(SPEC_START):
        return None  # e.g. a sentence in a comment: "# dev tools"
    return {"name": name, "extras": extras or "", "spec": spec, "marker": marker or ""}


def pinned_version(spec):
    """'==1.2.3' -> '1.2.3'. Ranges, wildcards and empty specs -> None."""
    m = re.fullmatch(r"===?([^,*]+)", spec or "")
    return m.group(1) if m else None


def add_source(found, req, file, line, group):
    key = normalize(req["name"])
    pkg = found["packages"].setdefault(key, {"name": key, "declared_as": req["name"], "sources": []})
    pkg["sources"].append({"file": file, "line": line, "group": group, **req})


def read_requirements(path, repo, found, seen):
    """Parse a requirements*.txt file, following '-r other.txt' includes."""
    path = path.resolve()
    if path in seen:
        return
    seen.add(path)
    rel = path.relative_to(repo).as_posix()
    found["dep_files"].append(rel)
    for n, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            req = parse_requirement(line.lstrip("#").strip())
            if req and req["spec"]:
                found["commented_out"].append({"file": rel, "line": n, "text": line})
            continue
        include = re.match(r"(?:-r|--requirement)[\s=]*(\S+)", line)
        if include:
            target = path.parent / include.group(1)
            if target.exists():
                read_requirements(target, repo, found, seen)
            else:
                found["errors"].append(f"{rel}:{n}: included file not found: {include.group(1)}")
            continue
        req = None if line.startswith("-") else parse_requirement(line)
        if req:
            add_source(found, req, rel, n, "requirements")
        else:
            found["skipped_lines"].append({"file": rel, "line": n, "text": line})


def read_pyproject(path, repo, found):
    rel = path.relative_to(repo).as_posix()
    found["dep_files"].append(rel)
    text = path.read_text(encoding="utf-8")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        found["errors"].append(f"{rel}: invalid TOML: {e}")
        return
    lines = text.splitlines()
    project = data.get("project", {})
    found["requires_python"] = project.get("requires-python")
    groups = [("project", project.get("dependencies", []))]
    groups += [(f"optional:{k}", v) for k, v in project.get("optional-dependencies", {}).items()]
    groups += [(f"group:{k}", v) for k, v in data.get("dependency-groups", {}).items()]
    groups += [("build-system", data.get("build-system", {}).get("requires", []))]
    for group, items in groups:
        for item in items:
            if not isinstance(item, str):
                continue  # e.g. {include-group = "test"}
            # tomllib gives no line numbers, so find the first line containing the string
            line = next((i for i, l in enumerate(lines, 1) if f'"{item}"' in l or f"'{item}'" in l), None)
            req = parse_requirement(item)
            if req:
                add_source(found, req, rel, line, group)
            else:
                found["skipped_lines"].append({"file": rel, "line": line, "text": item})


def is_excluded(rel, exclude):
    """True if repo-relative path `rel` is one of `exclude` or inside one of them."""
    return any(rel == e or rel.startswith(e.rstrip("/") + "/") for e in exclude)


def collect_declared(repo, exclude=()):
    """Every dependency declared in requirements*.txt and pyproject.toml (no network)."""
    repo = Path(repo).resolve()
    found = {"dep_files": [], "requires_python": None, "packages": {},
             "commented_out": [], "skipped_lines": [], "errors": []}
    seen = set()
    for path in sorted(repo.glob("requirements*.txt")) + sorted(repo.glob("requirements/*.txt")):
        if not is_excluded(path.relative_to(repo).as_posix(), exclude):
            read_requirements(path, repo, found, seen)
    if (repo / "pyproject.toml").exists():
        read_pyproject(repo / "pyproject.toml", repo, found)
    return found


def detect_target_python(repo, explicit):
    """Which Python the repo runs on: --target-python > .python-version > Dockerfile > this interpreter."""
    if explicit:
        return explicit, "--target-python"
    pv = repo / ".python-version"
    if pv.exists():
        m = re.match(r"(3\.\d+)", pv.read_text().strip())
        if m:
            return m.group(1), ".python-version"
    dockerfile = repo / "Dockerfile"
    if dockerfile.exists():
        m = re.search(r"^FROM\s+\S*python:(3\.\d+)", dockerfile.read_text(errors="replace"), re.M)
        if m:
            return m.group(1), "Dockerfile"
    return "%d.%d" % sys.version_info[:2], "running interpreter"


def version_key(v):
    return tuple(int(x) for x in v.split("."))


def fetch_json(url, accept="application/json"):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "dependency-exit-skill"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def days_since(iso, now):
    if not iso:
        return None
    return (now - datetime.fromisoformat(iso.replace("Z", "+00:00"))).days


def pypi_facts(name, pinned, now):
    """Facts from the PyPI JSON API (+ PEP 792 project status from the Simple API)."""
    quoted = urllib.parse.quote(name)
    data = fetch_json(f"{PYPI}/pypi/{quoted}/json")
    info = data["info"]
    all_releases = data.get("releases", {})
    # first upload time of each version that has files
    dates = {v: min(f["upload_time_iso_8601"] for f in files) for v, files in all_releases.items() if files}
    last_release = max(dates.values(), default=None)
    facts = {
        "latest_version": info["version"],
        "latest_version_date": dates.get(info["version"]),
        "last_release_date": last_release,
        "days_since_last_release": days_since(last_release, now),
        "total_releases": len(dates),
        "latest_requires_python": info.get("requires_python") or None,
        "summary": info.get("summary"),
        "project_urls": info.get("project_urls") or {},
    }
    classifiers = info.get("classifiers", [])
    if pinned:
        pinned_date = dates.get(pinned)
        facts.update({
            "pinned_version": pinned,
            "pinned_found_on_pypi": pinned in all_releases,
            "pinned_release_date": pinned_date,
            "releases_since_pinned": sum(1 for d in dates.values() if pinned_date and d > pinned_date),
            "pinned_yanked": any(f.get("yanked") for f in all_releases.get(pinned, [])),
        })
        if pinned in all_releases:
            pin_info = fetch_json(f"{PYPI}/pypi/{quoted}/{urllib.parse.quote(pinned)}/json")["info"]
            facts["pinned_requires_python"] = pin_info.get("requires_python") or None
            classifiers = pin_info.get("classifiers", [])
    facts["python_classifiers"] = sorted(
        {c.rsplit("::", 1)[1].strip() for c in classifiers if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", c)},
        key=version_key,
    )
    facts["development_status"] = next((c.split("::")[-1].strip() for c in classifiers if c.startswith("Development Status")), None)
    try:
        simple = fetch_json(f"{PYPI}/simple/{quoted}/", "application/vnd.pypi.simple.v1+json")
        facts["project_status"] = (simple.get("project-status") or {}).get("status")
    except (urllib.error.URLError, OSError, ValueError):
        facts["project_status"] = None
    return facts


def risk_flags(pkg, facts, stale_days, target_python):
    """Plain yes/no facts. No scoring: ranking is the AI's job."""
    # build-system requires are build-time only, so a different spec there is not a conflict
    specs = {s["spec"] for s in pkg["sources"] if s["group"] != "build-system"}
    pinned = pkg["pinned_version"]
    flags = {
        "exact_pin": pinned is not None,
        "unpinned": any(not s["spec"] for s in pkg["sources"]),
        "specs_differ": len(specs) > 1,
    }
    if facts and "error" not in facts:
        classifiers = facts["python_classifiers"]
        flags.update({
            "pinned_behind_latest": bool(pinned) and pinned != facts["latest_version"],
            "stale_last_release": (facts["days_since_last_release"] or 0) > stale_days,
            "pinned_yanked": facts.get("pinned_yanked", False),
            "pinned_not_on_pypi": bool(pinned) and not facts.get("pinned_found_on_pypi", True),
            "status_not_active": facts["project_status"] not in (None, "active"),
            # only meaningful when the package lists minor versions at all
            "python_classifiers_older_than_target": bool(classifiers)
                and version_key(classifiers[-1]) < version_key(target_python),
        })
    return flags


def scan(repo, offline=False, stale_days=730, target_python=None, exclude=()):
    repo = Path(repo).resolve()
    found = collect_declared(repo, exclude)
    target, target_source = detect_target_python(repo, target_python)
    now = datetime.now(timezone.utc)
    packages = sorted(found["packages"].values(), key=lambda p: p["name"])
    for pkg in packages:
        pkg["pinned_version"] = next((v for s in pkg["sources"] if (v := pinned_version(s["spec"]))), None)

    def lookup(pkg):
        if offline:
            return None
        try:
            return pypi_facts(pkg["name"], pkg["pinned_version"], now)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            return {"error": f"{type(e).__name__}: {e}"}

    with ThreadPoolExecutor(max_workers=8) as pool:
        for pkg, facts in zip(packages, pool.map(lookup, packages)):
            pkg["pypi"] = facts
            pkg["risk_flags"] = risk_flags(pkg, facts, stale_days, target)

    return {
        "tool": "scan_deps",
        "label": "OBSERVED",
        "repo": str(repo),
        "generated_at": now.isoformat(timespec="seconds"),
        "scanner_python": sys.version.split()[0],
        "target_python": target,
        "target_python_source": target_source,
        "stale_days": stale_days,
        "offline": offline,
        "dep_files": found["dep_files"],
        "requires_python": found["requires_python"],
        "packages": packages,
        "commented_out": found["commented_out"],
        "skipped_lines": found["skipped_lines"],
        "errors": found["errors"],
    }


def write_output(data, out):
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    else:
        print(text)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="repo to scan (default: current folder)")
    ap.add_argument("--out", help="write JSON here instead of printing it")
    ap.add_argument("--offline", action="store_true", help="skip PyPI, only parse files")
    ap.add_argument("--stale-days", type=int, default=730, help="flag packages with no release for this long")
    ap.add_argument("--target-python", help="Python version the repo runs on, e.g. 3.12 (auto-detected if omitted)")
    ap.add_argument("--exclude", nargs="*", default=[], help="repo-relative files/folders to ignore")
    args = ap.parse_args()
    if not Path(args.repo).is_dir():
        sys.exit(f"error: --repo {args.repo!r} is not a folder")
    write_output(scan(args.repo, args.offline, args.stale_days, args.target_python, args.exclude), args.out)


if __name__ == "__main__":
    main()
