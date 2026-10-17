# PR Analyser (resilience_agent) - architecture, setup and operating guide

`resilience_agent` is an evidence-driven, multi-agent maintenance system for Python repositories,
demonstrated on LinkWatch.
This guide documents what is implemented. Anything not implemented is listed under Known limitations.

## 1. Architecture

```
            Level 1: context engine                 Level 2: discovery council            Level 3: repair council
 repo ──► inspect ─► index (AST, deps, usages, ──► executed baseline ─► detectors ──► 3 voters ─► adjudicator ─► evidence rules
          git      PyPI, Graphify, DeepWiki MCP,    (py3.12/3.13 x                 (independent)                    │
                   Code Wiki import) ─► SQLite KB    requirements/pyproject)                                  HUMAN GATE 1
                                                                                                                    │
   capsule ◄─ evaluate ◄─ verify (worktree+venv) ◄─ apply ◄─ rank ◄─ revise ◄─ anonymous review ◄─ 3 proposers (A/B/C)
      │          └── fail: logs ─► proposer revises ─► re-review ─► re-rank (max 3 attempts)
      └─► HUMAN GATE 2 ─► prepare PR (push only with RA_ALLOW_PUBLISH=1) ─► Graphify refresh on accepted change
```

| Module | Role |
|---|---|
| `resilience_agent/context.py` | Level 1: git metadata, AST module/symbol inventory, dependency inventory and usage map (reuses the stdlib scanners in `.agents/skills/dependency-exit/scripts/`), DeepWiki via its real MCP server, Code Wiki import, Graphify CLI, provenance-tagged facts, query helpers |
| `resilience_agent/discovery.py` | Level 2: deterministic detectors, discovery/skeptic/evidence voters, adjudicator, deterministic classification rules |
| `resilience_agent/repair.py` | Level 3: proposers, anonymisation, reviews, deterministic ranking, issue-scoped weights, worktree patch application, issue-scoped acceptance gate |
| `resilience_agent/verification.py` | Restricted command runner, per-environment virtualenvs, check matrix, junit parsing, before/after comparison |
| `resilience_agent/workflow.py` | LangGraph state machines (typed state, SQLite checkpoints, `interrupt()` gates, resumable) |
| `resilience_agent/reports.py` | Dependency Exit Capsule (Markdown + JSON) and PR text |
| `resilience_agent/storage.py` | SQLite: facts, issues, votes, candidates, verifications, approvals |
| `resilience_agent/llm.py` | Ollama provider (structured JSON output, recorded settings) and a fake provider for unit tests only |
| `resilience_agent/cli.py`, `dashboard.py` | CLI and Streamlit dashboard over the same backend |
| `acceptance/linkwatch/` | Independent acceptance criteria owned by the verifier; repair candidates cannot edit them |

Knowledge-base source priority: 1 checked-out code and executed checks, 2 dependency metadata and tool output,
3 git history and docs, 4 Graphify, 5 DeepWiki / Code Wiki text, 6 LLM output. Each fact stores source type,
file/URL, line, commit, collection time, and whether it was extracted or inferred.

## 2. Setup

```powershell
pip install uv                      # or any way to get uv
uv python install 3.12 3.13         # interpreters used by the verifier
uv venv --python 3.12 .venv-agent
uv pip install --python .venv-agent\Scripts\python.exe -r requirements-dashboard.txt
# Ollama: https://ollama.com (Windows: winget install Ollama.Ollama)
ollama pull qwen2.5-coder:3b        # default; qwen2.5-coder:7b if the machine is fast enough
.venv-agent\Scripts\activate
python -m resilience_agent doctor
```

## 3. Environment variables

See `.env.example`. Main ones: `RA_MODEL`, `OLLAMA_HOST`, `RA_SEED`, `RA_NUM_CTX`, `RA_PYTHONS`,
`RA_INSTALL_MODES`, `RA_MAX_ATTEMPTS`, `RA_REVISION_ROUNDS`, `RA_COUNCIL_LIMIT`, `RA_COMMAND_TIMEOUT`,
`RA_OFFLINE`, `RA_DEEPWIKI_REPO`, `RA_ALLOW_PUBLISH`. No credentials are read by the agent; the GitHub CLI
uses its own login only when publishing is explicitly allowed.

## 4. CLI

| Command | What it does |
|---|---|
| `doctor` | Real probes: Ollama + model, DeepWiki MCP, Code Wiki import, Graphify, Docker, GitHub CLI, Python 3.12/3.13, uv |
| `index` | Level 1 index, writes `.resilience/artifacts/index/<commit>/` |
| `baseline [--refresh]` | Executed baseline on the committed HEAD |
| `scan [--fresh TAG]` | Discovery LangGraph: inspect, index, baseline, detectors, council, adjudication |
| `issues` / `inspect ID` | Issue table / evidence, votes, disagreement, rule notes |
| `approve ID [--scope a,b]` / `reject ID` / `investigate ID` | Human gate 1 (persisted) |
| `repair ID [--new]` | Repair LangGraph; pauses at gates, resumes from checkpoints |
| `verify ID [--candidate C]` | Re-run the verifier on an applied candidate |
| `report ID` | Print the capsule |
| `publish ID` / `reject-publication ID` | Human gate 2, then resume the workflow |
| `query where-used/modules/tests/impact/migration-files/feature X` | Knowledge-base queries |
| `run` | Guided pipeline that asks the person at the terminal at each gate |

## 5. Councils

**Discovery.** Detectors create one candidate per dependency with numbered evidence (`E1..`): manifest
declarations, PyPI facts, AST findings, and executed baseline results (failing checks attributed to the
package whose stack frame raised, deprecation warnings, install paths that resolve different versions,
tests whose outcome depends on the install path). The top `RA_COUNCIL_LIMIT` candidates go to three
voters that see the identical packet and never each other's output (discovery: recall, temperature 0.7;
skeptic: precision, 0.2; evidence: proof, 0.1). The adjudicator sees all votes. Then deterministic rules decide:
cited evidence ids must exist; SUPPORTED needs valid evidence; runtime claims need executed evidence; a dismissal
is overridden when executed failure evidence exists; unanimous rejection without executed evidence is UNSUPPORTED.
Votes are treated as correlated opinions, not independent proof.

**Repair.** Proposers A (minimal), B (migration), C (alternative) return exact find/replace edits (not whole-file
rewrites: a 3B model truncated `pyproject.toml` that way), limited to the manifests and the files that use the scoped
dependencies (never tests). The validator rejects edits whose `find` text is not unique, results that do not parse
(TOML / Python), files that lose more than half their lines, and manifests that end up declaring a scoped package with
different version specs; the proposer gets one self-correction round with the exact validator messages. Model output is
capped (`RA_NUM_PREDICT`) and requests time out (`RA_LLM_TIMEOUT`). Each reviewer sees the other candidates'
diffs under random labels (`Proposal-P/Q/R`) and scores nine criteria with specific defects. Proposers revise once.
Ranking is deterministic: weighted criteria x reviewer weight, minus defect and reject penalties; ties go to the
smaller diff. After verification, issue-scoped weights change (proposer +0.2/-0.4; reviewer +/-0.1 only when the
verifier confirms or refutes their verdict; clipped 0.2-2.0). Weights live in the repair thread's state and reset
for every new issue/thread; history is kept in the capsule. Model weights are never changed (seed and temperature recorded).
Reviewer labels such as `P` or `proposal p` are normalised to `Proposal-P` before scoring.

**Retries after a failed verification** (max `RA_MAX_ATTEMPTS`): the proposer receives the gate reasons, every manifest
`file:line` that declares the scoped package, each distinct failing test/error once with its environments, regressions,
and the reviewers' specific defects; its previous patch is shown as "THIS FAILED, do not repeat it"; each retry uses a
different seed and a higher temperature; a diff identical to one that already failed is rejected and never ranked.
A new `repair --new` run archives the previous run's candidates, reviews and verifications.

## 6. Verification design

For every Python in `RA_PYTHONS` and both install paths (`requirements*.txt` and `pip install .` from
`pyproject.toml`), in a fresh virtualenv: install, `uv pip check`, import of every package module, CLI `--help`
for each `[project.scripts]` entry, the project's pytest suite (junit parsed per test, warnings captured), and the
independent acceptance suite. The issue-scoped gate passes only when: required Python 3.12 was tested; no check or
test fails that passed in the baseline; the issue's target tests pass everywhere; the scoped dependencies resolve
to the same version on every install path. Pre-existing failures outside the scope are listed, not hidden, and
`fully_green` reports whether literally everything passed. Missing interpreters are reported as not tested.

## 7. Human approval policy

Gate 1: no repair runs until a maintainer approves the issue (`approve`, dashboard button, or `run` prompt).
Gate 2: a verified patch stays on its local `ra/...` branch until a maintainer runs `publish`. Even then the
branch is pushed and a draft PR created only with `RA_ALLOW_PUBLISH=1`; otherwise the exact commands are written to
`pr.json`. The default branch is never modified. Tests use a clearly labelled fixture approval.

## 8. Dependency Exit Capsule

`.resilience/artifacts/capsules/<ISSUE>/capsule.md` + `capsule.json` + `pr.md` + `selected.patch`, containing:
original declarations and PyPI facts, why migration was considered (evidence), usage sites and API symbols, council
votes and adjudication, all candidates (with validation problems), anonymous reviews, ranking and weight history,
selected approach and the actual diff, tests before and after (environment matrix and failing tests), regressions
and fixes, remaining risks and gaps, rollback procedure, PR summary, log paths and reproduction commands. Sections
are labelled OBSERVED / VERIFIED / AI.

## 9. Safety

Allow-listed executables (python, uv, git, graphify), no shell, cwd confined to the repo and `.resilience/`,
timeouts, secret-looking environment variables removed, model and wiki text treated as data, patches confined to
allowed files inside their worktree. Isolation is a reduced-trust local mode: a git worktree plus a virtualenv is
not a security sandbox.

## 10. Known limitations

- Docker sandboxing is not implemented; Docker was not running on the build machine. Runs use the reduced-trust local mode.
- DeepWiki integration is real (MCP), but neither the fork nor rishrek/LinkWatch is indexed on DeepWiki yet, so it reports `not_indexed`.
- Google Code Wiki has no documented public API; only manual import of saved pages into `.resilience/codewiki/` is supported.
- Default model is qwen2.5-coder:3b because the demo laptop has no GPU (about 6 tokens/s); quality is lower than 7b.
- Failure attribution to packages uses stack frames and install-path differences; tests failing in every environment
  are attributed coarsely to every package with an executed crash.
- Acceptance criteria exist only for LinkWatch (`acceptance/linkwatch/`). Other repos get the generic checks.
- Poetry and setup.py/setup.cfg dependency lists are not parsed; dynamic imports are not detected.

## 11. Demo

```powershell
python -m resilience_agent doctor
python -m resilience_agent scan                 # Level 1 + baseline + discovery council
python -m resilience_agent inspect RA-pydantic
python -m resilience_agent approve RA-pydantic --scope pydantic,jinja2   # maintainer decision
python -m resilience_agent repair RA-pydantic   # repair council + verification + capsule, pauses at gate 2
python -m resilience_agent report RA-pydantic
streamlit run resilience_agent/dashboard.py     # same data, visual
```

## 12. Review mode

`python -m resilience_agent --repo PATH review --base main --head BRANCH [--pr-description FILE] [--full-suite] [--gen-tests N] [--no-ai]`

Graph (`workflow.review_graph`): `prepare_review -> map_blast_radius -> structural_checks -> verify_base -> verify_head
-> behaviour_diff -> intent_review -> decide_verdict -> generate_review_capsule`. Maintenance graphs are unchanged.

- **Worktrees:** base and head are checked out detached in `.resilience/worktrees/review-<sha>`; the repo is never modified.
- **Venvs:** review uses `.resilience/venvs-review` (with pytest-cov) and reinstalls the project per tree in `package`
  mode, so a cached venv never tests the other side's code. Maintenance venvs are untouched.
- **Blast radius (`review.py`):** `git diff -U0` line ranges -> innermost function/class on each side. Static
  [OBSERVED]: stdlib-AST reference graph (calls, attribute references, decorator registration such as
  `@cli.command()`), plus Graphify `calls` edges when available, walked backwards 3 levels. Dynamic [VERIFIED]:
  `pytest --cov-context=test` on base and head; a test is affected if it executed a changed line. Items found by only
  one source are `uncertain`. Coverage % = affected functions executed by at least one test.
- **Test selection:** affected test files run on both sides; the full suite runs with `--full-suite`, when no affected
  test is found, or when the diff touches conftest/pytest/packaging config.
- **Structure (`structure.py`, OBSERVED):** griffe breaking changes (static load, code is never imported), likely
  renames (same module, kind, signature, body >= 80% identical), new import cycles (Tarjan SCC over module-level
  imports), layer rules from `pr-analyser.toml` read from BASE, packaging (entry points, declarations, new undeclared imports).
  ```toml
  [[forbid]]
  source = "pkg.cli"
  target = "pkg.cache"
  ```
- **Behaviour (`behaviour.py`, VERIFIED):** pure-enough module-level functions in the blast radius (no IO, clock,
  randomness, globals, also through internal callees) get the arguments recorded during base's tests replayed on base
  twice (unstable results are dropped) and on head. `--gen-tests N` asks the model for tests written only to
  `.resilience/generated_tests/<review>/`; a test counts only if it passes on base.
- **Intent (AI):** one model call compares the PR description with the diff; mismatches that cite no changed file are
  dropped. AI output is never an input to the verdict.
- **Verdict:** `UNVERIFIABLE` (a side could not be tested) > `REGRESSION_FOUND` > `UNVERIFIABLE` (no test reaches the
  change) > `NEEDS_HUMAN` (API break, new cycle, layer violation, undeclared import, removed entry point, behaviour diff)
  > `FIXED` > `SAFE`.

## 13. Decision log

`history.py` writes `.resilience/history/<issue-or-review>/<entry>.json` + `.md` after every repair and review, with
raw checker outputs in `<entry>-raw/`. Every attempt, not-done item and human gate decision carries a `ref` that must
exist (file path or `approval:<id>`); `save()` refuses otherwise. Text is scrubbed of secrets first. On the next
repair of the same issue, failed approaches younger than `RA_HISTORY_MAX_AGE_DAYS` (default 30) are given to the
proposers and reviewers as "do not repeat" and identical patches are rejected; older ones become "retest".
