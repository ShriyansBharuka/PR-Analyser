# PR Analyser
### AI-Powered Pull Request Analysis, Code Repair, and Verification

**Understand the issue. Generate a fix. Verify the changes. Review with confidence.**

PR Analyser is an AI-powered tool designed to help developers identify Python dependency issues, analyse code failures, generate potential fixes, and validate changes through automated testing.

By combining AI-assisted code analysis with isolated repair workflows and human review, PR Analyser aims to make code maintenance more efficient, transparent, and reliable. It helps developers understand not only what changed, but also why a repair was proposed and whether the available test evidence supports it.

## ✨ Key Features

### 🔍 Intelligent Issue Analysis
Analyse Python repositories, dependency configurations, and error messages to identify potential causes of failures, including incompatible package versions, broken imports, deprecated APIs, and configuration problems.

### 🤖 AI-Powered Code Repair
Generate candidate patches for identified issues using a code-focused language model. Multiple repair candidates can be evaluated to help identify a suitable solution rather than blindly accepting the first suggestion.

### 🧪 Automated Verification
Validate proposed changes using dependency checks, Python environments, and automated tests. Verification results provide evidence of whether the original issue was resolved and whether additional failures appeared.

### 📜 Repair History
Maintain a history of repair attempts, including the original error, proposed changes, modified files, test outcomes, and final status. This makes it easier to revisit previous fixes, investigate unsuccessful attempts, and understand the repair process.

### 👨‍💻 Human-in-the-Loop Review
Keep developers in control of important decisions. Proposed fixes and their verification results can be reviewed before changes are accepted.

### 🌿 Isolated Repair Workspaces
Use Git worktrees to separate repair candidates from the main working tree, allowing proposed changes to be tested with less risk of interfering with ongoing development.

### 📊 Structured Logs and Reports
Capture useful repair and verification details in structured records, making failures easier to investigate and results easier to review.

### 🛡️ Safety-Oriented Execution
Use safeguards such as restricted command execution, timeouts, patch validation, secret scrubbing, and approval gates to reduce the risks of automated code modification.

## ⚙️ How It Works

1. **Analyse** — Inspect the repository, dependency configuration, and reported error.
2. **Diagnose** — Identify likely causes of the failure.
3. **Generate** — Produce one or more candidate fixes.
4. **Isolate** — Apply proposed changes in separate workspaces.
5. **Verify** — Run relevant checks and automated tests.
6. **Evaluate** — Compare candidate results and identify promising fixes.
7. **Review** — Present code differences and verification evidence for human inspection.
8. **Record** — Preserve repair outcomes for future reference.

The objective is to make AI-assisted repair a structured, reviewable process rather than an unexplained code-generation step.

## 🧰 Technology Stack

| Technology | Purpose |
|---|---|
| Python | Core application and repair logic |
| LangGraph | Workflow orchestration |
| Ollama | Local language-model execution |
| Qwen2.5-Coder | Code analysis and patch generation |
| Pydantic | Structured data validation |
| SQLite | Workflow checkpoints and state persistence |
| Git worktrees | Isolated repair environments |
| `uv` | Dependency and environment management |
| `pytest` | Automated testing |
| Streamlit | User interface |
| Graphify | Repository and code relationship analysis |
| DeepWiki MCP | Repository context integration, where configured |
| GitHub Actions | Continuous integration |

## 🚀 Getting Started

### Prerequisites

- Python
- Git
- `uv`
- Ollama
- A compatible code model
- Project dependencies installed according to the repository configuration

### Installation

Clone the repository:

```bash
git clone <your-repository-url>
cd <your-repository-directory>
```

Install the project dependencies:

```bash
uv sync
```

Download the code model if using Qwen2.5-Coder 3B with Ollama:

```bash
ollama pull qwen2.5-coder:3b
```

Run the test suite:

```bash
uv run pytest
```

If the Streamlit dashboard is configured at `dashboard.py`, launch it with:

```bash
uv run streamlit run dashboard.py
```

*Update the repository URL, model identifier, and dashboard entry point to match your actual project configuration.*

## 🧪 Testing and Reliability

PR Analyser uses automated checks to evaluate proposed repairs. A successful test run is useful evidence, but it does not guarantee that every regression or edge case has been eliminated.

Repair quality can vary depending on the selected model, available repository context, dependency complexity, and test coverage. Human review remains an important part of the process.







---

**PR Analyser — Making code analysis and repair more transparent, testable, and developer-friendly.**
