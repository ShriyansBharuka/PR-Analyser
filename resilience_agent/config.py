"""Runtime settings. Every value can be overridden with an RA_* environment variable."""
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The agent lives in the same repo as the code it maintains, so it must not analyse itself.
AGENT_PATHS = ["resilience_agent", "tests_agent", "acceptance", "demo", "docs", ".agents", ".github",
               "requirements-agent.txt", "requirements-dashboard.txt", "graphify-out"]
SKILL_SCRIPTS = ROOT / ".agents" / "skills" / "dependency-exit" / "scripts"
ACCEPTANCE_DIR = ROOT / "acceptance"


def _env(name, default):
    return os.environ.get(name, default)


@dataclass
class Settings:
    repo: Path = field(default_factory=lambda: Path(_env("RA_REPO", ".")).resolve())
    provider: str = field(default_factory=lambda: _env("RA_PROVIDER", "ollama"))  # ollama | fake
    # 3b is the default because the demo laptop is CPU-only; RA_MODEL=qwen2.5-coder:7b on better hardware
    model: str = field(default_factory=lambda: _env("RA_MODEL", "qwen2.5-coder:3b"))
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_HOST", "http://127.0.0.1:11434"))
    num_ctx: int = field(default_factory=lambda: int(_env("RA_NUM_CTX", "8192")))
    num_predict: int = field(default_factory=lambda: int(_env("RA_NUM_PREDICT", "1500")))  # caps runaway output
    seed: int = field(default_factory=lambda: int(_env("RA_SEED", "7")))
    llm_timeout: float = field(default_factory=lambda: float(_env("RA_LLM_TIMEOUT", "420")))
    pythons: list = field(default_factory=lambda: _env("RA_PYTHONS", "3.12,3.13").split(","))
    install_modes: list = field(default_factory=lambda: _env("RA_INSTALL_MODES", "requirements,package").split(","))
    max_attempts: int = field(default_factory=lambda: int(_env("RA_MAX_ATTEMPTS", "3")))
    revision_rounds: int = field(default_factory=lambda: int(_env("RA_REVISION_ROUNDS", "1")))
    council_limit: int = field(default_factory=lambda: int(_env("RA_COUNCIL_LIMIT", "4")))
    command_timeout: int = field(default_factory=lambda: int(_env("RA_COMMAND_TIMEOUT", "600")))
    offline: bool = field(default_factory=lambda: _env("RA_OFFLINE", "0") == "1")
    deepwiki_repo: str = field(default_factory=lambda: _env("RA_DEEPWIKI_REPO", ""))  # e.g. rishrek/LinkWatch
    exclude: list = field(default_factory=lambda: list(AGENT_PATHS))
    # decision-log entries older than this become "retest" instead of "skip"
    history_max_age_days: int = field(default_factory=lambda: int(_env("RA_HISTORY_MAX_AGE_DAYS", "30")))

    @property
    def work(self) -> Path:
        path = self.repo / ".resilience"
        path.mkdir(exist_ok=True)
        return path

    def dir(self, name) -> Path:
        path = self.work / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def db(self):
        from .storage import DB
        return DB(self.work / "agent.sqlite")
