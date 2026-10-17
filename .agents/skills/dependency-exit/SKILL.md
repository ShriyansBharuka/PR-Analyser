---
name: dependency-exit
description: Audits a Python repo's dependencies for outdated, deprecated, unmaintained or risky packages, maps where each one is used in the code, proposes and applies a migration, runs the tests, and writes a Dependency Exit Capsule that separates observed facts from AI suggestions. Use when asked to check, upgrade, replace, remove or de-risk Python dependencies.
license: MIT
compatibility: Needs Python 3.11+ (scripts use the standard library only), network access to pypi.org, and a terminal tool. The target repo may need its own Python version to run its tests.
metadata:
  author: "resilience-agent"
  version: "0.1.0"
---

# Dependency Exit

Find risky dependencies in a Python repo, plan their exit (upgrade, replace, remove),
apply it, prove it with tests, and write a **Dependency Exit Capsule**.

Scripts collect facts. You do the reasoning. Never mix the two up.

## Labels (use them everywhere you write)

- **OBSERVED**: came from a script's JSON output or from reading a file. Quote the field or `file:line`.
- **VERIFIED**: came from running something (tests, imports) and looking at the result.
- **AI-SUGGESTED**: your own analysis, recommendation or guess. Always say so.

## Paths

- `SKILL_DIR`: the folder containing this SKILL.md.
- `REPO`: the repo to audit (default: the current working directory).
- `OUT`: `REPO/dependency-exit-out` (create it if missing).

Run scripts with any Python 3.11+ (`python3`, or `py -3.11` on Windows).

## Procedure

### 1. Scan declared dependencies (OBSERVED)

```
python SKILL_DIR/scripts/scan_deps.py --repo REPO --out OUT/scan.json
```

Reads `requirements*.txt` and `pyproject.toml` and asks PyPI about every package.
Each package has `sources` (where it is declared), `pypi` (facts) and `risk_flags`.
If PyPI is unreachable, `pypi.error` is set. Report that and do not guess the missing facts.

### 2. Find where each dependency is used (OBSERVED)

```
python SKILL_DIR/scripts/find_usages.py --repo REPO --out OUT/usages.json
```

Per package: `usages` (file:line), `api_symbols`, `private_api`, `monkeypatches`,
`text_mentions` (Makefile, Dockerfile, CI files) and `covering_tests`.
Top-level: `undeclared_imports`, `declared_unused`, `unmapped_imports`.

### 3. Analyze (AI-SUGGESTED)

Go through packages one at a time, starting with the ones that have the most `risk_flags`
or appear in `undeclared_imports` / `declared_unused`. For each one:

1. Read its entries in `scan.json` and `usages.json`.
2. Open the files in its `usages` to see how it is really used.
3. Pick a verdict: `keep`, `upgrade`, `replace`, `replace-with-stdlib`, `remove`, or `declare` (for undeclared imports).
4. Cite the exact JSON fields or `file:line` that support the verdict. If your reason comes
   from your own knowledge (e.g. "this project is deprecated upstream"), mark it as such
   and set `confidence` lower.

Write all verdicts to `OUT/analysis.json` in exactly this shape:

```json
{
  "model": "<model name and tag you are running as>",
  "packages": [
    {
      "package": "examplepkg",
      "verdict": "replace",
      "target": "otherpkg>=2.0",
      "confidence": "high | medium | low",
      "evidence": ["scan.json: risk_flags.stale_latest_release", "src/app.py:4"],
      "reasoning": "short plain-English reason",
      "migration_steps": ["..."],
      "files_to_change": ["requirements.txt", "src/app.py"],
      "suggested_tests": ["tests/test_app.py"],
      "risks": ["..."]
    }
  ]
}
```

The values above are an example of the format only. Fill them from the real JSON.
Use `covering_tests` from `usages.json` for `suggested_tests`. If it is empty, say no test
covers this dependency and propose one.

### 4. Migrate (AI-SUGGESTED, then checked in step 5)

- Work on a new git branch: `git checkout -b dependency-exit/<short-name>`.
- Change one dependency at a time. Keep diffs small.
- Update every place it is declared (all `sources`), then the code in its `usages`.
- Do not touch unrelated code. Do not fix security issues unless asked.

### 5. Verify (VERIFIED)

If `SKILL_DIR/scripts/run_tests.py` exists:

```
python SKILL_DIR/scripts/run_tests.py --repo REPO --out OUT/tests.json
```

Otherwise run the repo's tests yourself and save the full output:

```
python -m pytest -q > OUT/pytest_output.txt 2>&1
```

Use the Python version the repo supports (see `requires_python` in `scan.json`).
If tests fail, read the traceback, explain the cause (label it AI-SUGGESTED), fix
if it is caused by your migration, and run again. Record every run, not only the last one.

### 6. Build the capsule

If `SKILL_DIR/scripts/build_capsule.py` exists:

```
python SKILL_DIR/scripts/build_capsule.py --out-dir OUT
```

Otherwise write `OUT/capsule.md` by hand with these sections, in order:
problem identified, dependency/repo details, risk indicators, usage locations,
AI analysis and recommendations, proposed changes, suggested tests,
executed test results, compatibility/maintenance gaps, unresolved concerns and limitations.
Mark every section or bullet as OBSERVED, VERIFIED or AI-SUGGESTED.

## Rules

- Never invent version numbers, dates or test results. Use only what the JSON or a command showed.
- If a step failed or was skipped, write that in the capsule. Do not hide it.
- If a script crashes, show its error and continue with what you have.
- If unsure, say "unverified" instead of guessing.
- Keep your replies short. Put details in `analysis.json` and the capsule.
