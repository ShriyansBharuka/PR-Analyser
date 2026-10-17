# PR Analyser

**AI that shows its evidence.** An open-weight, local-first agent that checks incoming changes and repairs Python dependency problems, then proves every claim by running real tests.

Most tools say "this version is old". PR Analyser finds problems that only appear when the code runs, tries fixes in isolation, verifies them with real tests, and hands a human a ready-to-review package. Every statement it makes is labelled by how it is known.

Tested on [LinkWatch](https://github.com/rishrek/LinkWatch), a Python CLI that checks website uptime.

---

## Three kinds of truth

Every claim in every report carries one label:

| Label | Meaning | Examples |
|---|---|---|
| **OBSERVED** | Read directly from the code, git history or registries | Imports, pinned versions, PyPI release dates, usage sites |
| **VERIFIED** | Proven by actually running something | Install, `uv pip check`, import smoke test, CLI `--help`, pytest, acceptance suite |
| **AI** | An opinion from the language model | Council votes, proposed patches, reviewer scores |

**Rule: AI never overrides VERIFIED.** Deterministic rules outrank AI consensus. A dismissal is overridden when executed failure evidence exists, and a claim of a runtime problem is rejected unless a run backs it.

---

## Two modes

### Maintenance mode (implemented)
Finds and fixes dependency rot in a repository: stale pins, manifests that disagree, removed APIs, silent behaviour changes.

### Review mode (in development)
Checks an incoming pull request instead of the whole repository:

- **Blast radius:** which modules, symbols and tests a change touches, using the AST and the Graphify knowledge graph.
- **Structure checks:** removed or renamed public symbols, broken imports and changed signatures introduced by the diff.
- **Evidence-labelled review:** each finding is tagged OBSERVED, VERIFIED or AI, and verification re-runs the affected tests.

Review mode reuses the same context engine, verifier and capsule format as maintenance mode.

---

## How it works

```mermaid
flowchart LR
    A[Context<br/>AST, deps, git,<br/>PyPI, Graphify] --> B[Executed baseline<br/>py3.12 + 3.13<br/>2 install paths]
    B --> C[Discovery council<br/>3 voters + adjudicator<br/>+ evidence rules]
    C --> G1{{Human Gate 1}}
    G1 --> D[Repair council<br/>3 proposers<br/>anonymous review]
    D --> E[Verifier<br/>isolated worktree + venv<br/>retry up to 3x]
    E --> F[Exit Capsule]
    F --> G2{{Human Gate 2}}
```

1. **Context.** Reads the repo with AST parsing, dependency files, git history, PyPI metadata and a Graphify knowledge graph. DeepWiki and Google Code Wiki context are optional.
2. **Baseline.** Runs the project as it stands, on Python 3.12 and 3.13 through both `requirements.txt` and `pyproject.toml`. This is VERIFIED evidence.
3. **Discovery council.** Deterministic detectors build candidate issues with numbered evidence. Three voters see the same packet and never each other's output: *Discovery* (recall), *Skeptic* (precision) and *Evidence* (proof). An adjudicator consolidates, then deterministic rules have the final word.
4. **Human Gate 1.** A person approves the issue and its scope. Nothing is repaired before this.
5. **Repair council.** Three proposers (minimal, migration, alternative) each return exact find/replace edits in an isolated git worktree. Candidates review each other anonymously, and ranking is deterministic.
6. **Verifier.** Install, `uv pip check`, import of every module, CLI `--help`, the project's pytest suite and an independent acceptance suite that candidates cannot edit. On failure the logs go back to the proposer, up to 3 attempts, otherwise `NO_VERIFIED_SOLUTION`.
7. **Exit Capsule and Human Gate 2.** A person approves publication. Nothing is pushed unless `RA_ALLOW_PUBLISH=1` is set.

---

## Key features

**Executed evidence, not guesses.** The baseline run exposes problems static tools miss, such as an install path that silently resolves a different major version.

**An AI council that is held to the evidence.** Three voters with different jobs, an adjudicator, and rules that make sure cited evidence exists and runtime claims have a run behind them.

**Competing repairs with anonymous review.** Reviewers see other candidates under random labels and score nine criteria. Reviewer and proposer weights change only when the verifier confirms or refutes a verdict.

**A decision log, not just a history.** The Exit Capsule records what was done and what was not:

- every candidate, including **rejected ones and the reason each lost** (validation problems, review defects, regressions)
- the council votes, disagreements and rule overrides
- **human decisions**: approvals, scope choices and maintainer guidance, labelled HUMAN
- ranking and weight history, tests before and after, remaining risks and gaps
- rollback steps and reproduction commands

Within a run, a patch identical to one that already failed is rejected and never ranked, and the proposer is told "THIS FAILED, do not repeat it". Carrying failed fixes across runs is on the roadmap.

**Safe by construction.** Allow-listed commands, no shell, timeouts, secrets scrubbed from the environment, patches confined to allowed files, and no ability to edit tests.

**Local and free to run.** Open-weight model on CPU through Ollama, with seed, temperature, tokens and raw model calls recorded for every call.

---

## Results on LinkWatch

Real runs on 17 Oct 2026. Logs and capsules are in [`demo/`](demo/).

| Step | Result |
|---|---|
| Baseline (VERIFIED) | Fails in all 4 environments (Python 3.12 and 3.13, both install paths). Jinja2 2.11.3 cannot import `soft_unicode`, which was removed in MarkupSafe 2.1. |
| Hidden behaviour change (VERIFIED) | `pyproject.toml` resolves Pydantic 2.14.1 while `requirements.txt` pins 1.10.13. Pydantic v2 silently ignores the v1 `anystr_strip_whitespace` setting, so site names and URLs stop being stripped. The acceptance test fails only on the `pyproject.toml` path. **Only execution caught this.** |
| Static analysis | 15 modules, 14 dependencies, 169 facts, 13 candidate issues. |
| Discovery council | The voters disagreed (Discovery said real issue, Skeptic and Evidence dissented). The evidence rules kept Jinja2 and Pydantic as supported because executed failures backed them, and downgraded two weak issues to needs-investigation. |
| `RA-jinja2` | **VERIFIED_RESOLUTION** on attempt 3: `Jinja2==3.1.6` in both manifests. 18 checks and tests fixed, 0 regressions ([patch](demo/jinja2-capsule/selected.patch), [capsule](demo/jinja2-capsule/capsule.md)). |
| `RA-pydantic` | **NO_VERIFIED_SOLUTION** in runs 2, 5 and 7. The 3B model only re-pinned Pydantic and never migrated `linkwatch/config.py`; the verifier blocked each candidate for 2 regressions ([run 5 capsule](demo/run5-capsule/capsule.md)). The agent reported failure instead of presenting an unproven patch. |

Failures that improved the agent: the 3B model truncated `pyproject.toml` (now exact find/replace edits with parser and truncation guards), one generation ran over 15 minutes (now output caps and hard timeouts), early candidates only aligned version pins and tests caught two regressions, and an identical failed patch was resubmitted (now rejected as a duplicate).

Speed on an i5 with 16 GB RAM and no GPU: about 2.5 minutes per council vote and about 2 minutes per verification run.

---

## Technology stack

| Technology | Purpose |
|---|---|
| Python 3.12+ | Core logic |
| LangGraph 1.x | Workflow orchestration with SQLite checkpoints and `interrupt()` human gates |
| Ollama + Qwen2.5-Coder 3B (7B optional) | Local voters, proposers and reviewers |
| Pydantic 2 | Structured, schema-constrained model output and logs |
| SQLite | Facts, issues, votes, candidates, verifications, approvals |
| uv, git worktrees, pytest | Isolated environments, patches and verification |
| Graphify | Code relationship graph |
| DeepWiki MCP | Optional repository context |
| Streamlit | Dashboard |
| GitHub Actions | CI |

**Model licences:** Qwen2.5-Coder-3B is under the Qwen Research License (research and non-commercial use). The 7B model is Apache-2.0. Set `RA_MODEL` to switch.

---

## Getting started

Prerequisites: Python 3.12+, Git, [uv](https://docs.astral.sh/uv/), [Ollama](https://ollama.com) and Python 3.12 and 3.13 interpreters for the verifier.

```bash


uv python install 3.12 3.13
uv venv --python 3.12 .venv-agent
uv pip install --python .venv-agent/bin/python -r requirements-dashboard.txt   # Windows: .venv-agent\Scripts\python.exe
ollama pull qwen2.5-coder:3b
source .venv-agent/bin/activate                                                # Windows: .venv-agent\Scripts\activate
```

Run the pipeline:

```bash
python -m resilience_agent doctor                  # real probes: Ollama, Graphify, Docker, uv, Pythons
python -m resilience_agent scan                    # context + baseline + discovery council
python -m resilience_agent inspect RA-pydantic     # evidence, votes, disagreement
python -m resilience_agent approve RA-pydantic --scope pydantic,jinja2   # Human Gate 1
python -m resilience_agent repair RA-pydantic      # repair council + verification + capsule
python -m resilience_agent report RA-pydantic
python -m resilience_agent publish RA-pydantic     # Human Gate 2
```

Dashboard and tests:

```bash
streamlit run resilience_agent/dashboard.py
python -m pytest tests_agent -q                    # fake provider, no model needed
```

Settings are environment variables prefixed `RA_` (see `.env.example`). Full architecture, councils, verification design and capsule spec are in [`docs/agent/GUIDE.md`](docs/agent/GUIDE.md).

---

## Limitations

- **No Docker sandbox yet.** Isolation is a git worktree plus a virtualenv, which is reduced-trust local mode and not a security sandbox. Do not run it on untrusted repositories.
- **Tested on one repository.** The results above are from LinkWatch only, and the independent acceptance suite exists only for LinkWatch. Other repos get the generic checks.
- **Small-model quality varies.** The 3B model failed the Pydantic migration. The 7B model works but was about 12 minutes per call on our CPU.
- **Proof is only as good as the tests.** A passing run is evidence, not a guarantee. Gaps in test coverage are gaps in verification.
- **Detection gaps.** Poetry and `setup.py` dependency lists are not parsed, and dynamic imports are not detected.
- **Context sources.** LinkWatch is not indexed in DeepWiki, and Google Code Wiki works only through manual import.
- **Review mode is in development** (see above).

---

## Roadmap

- Run on 5 to 10 more Python repos with a scoreboard for precision, recall, fix success rate and time.
- Council ablation: one prompt versus three voters versus three voters plus adjudicator.
- Docker isolation with network-off test runs.
- OSV security advisories, transitive dependencies and behaviour diffs between major versions.
- Deterministic migration recipes for known upgrades such as Pydantic v1 to v2.
- Reuse failed patches across runs, and a GitHub Action that opens PRs with the capsule attached.

---

## Links


- Target repo: [github.com/rishrek/LinkWatch](https://github.com/rishrek/LinkWatch)
- Agent Skills spec: [agentskills.io/specification](https://agentskills.io/specification)
- Inspiration for the anonymous review council: [github.com/karpathy/llm-council](https://github.com/karpathy/llm-council)

Licence: MIT.
