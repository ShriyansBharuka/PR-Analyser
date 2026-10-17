"""Real-model integration test. Skipped unless RA_LIVE=1 and Ollama is running with the configured model.
Run: RA_LIVE=1 python -m pytest tests_agent/test_live_model.py -q"""
import os

import pytest
from pydantic import BaseModel

from resilience_agent.config import Settings
from resilience_agent.llm import OllamaProvider

pytestmark = pytest.mark.skipif(os.environ.get("RA_LIVE") != "1", reason="live model test (set RA_LIVE=1)")


class Answer(BaseModel):
    package: str
    replacement_module: str


def test_real_model_returns_schema_valid_reasoning(tmp_path):
    p = OllamaProvider(Settings(), log_dir=tmp_path)
    health = p.health()
    assert health["status"] == "ready", health
    out, meta = p.complete("live-test", "Reply with JSON only.",
                           "A Python 3.12 project imports the third-party package `toml` only to call toml.load(). "
                           "Which standard-library module replaces it? Give package and replacement_module.",
                           Answer, temperature=0.0, tag="live")
    assert meta["fake"] is False and meta["output_tokens"]
    assert out.replacement_module.lower().startswith("tomllib")
    assert list(tmp_path.glob("*.json"))  # the raw exchange is logged as evidence
