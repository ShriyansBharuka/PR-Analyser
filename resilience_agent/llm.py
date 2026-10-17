"""Model providers. Every call returns a validated Pydantic object plus metadata that is stored as evidence.

OllamaProvider talks to a local Ollama server (open-weight models).
FakeProvider is deterministic and exists ONLY for unit tests; its outputs are labelled fake.
"""
import json
import time
from pathlib import Path

import httpx
from pydantic import BaseModel, ValidationError


class LLMError(RuntimeError):
    """The model could not produce a valid answer (separate from repository-analysis errors)."""


class OllamaProvider:
    name = "ollama"
    fake = False

    def __init__(self, settings, log_dir: Path | None = None):
        self.s = settings
        self.log_dir = log_dir
        self._digest = None

    def health(self):
        try:
            r = httpx.get(f"{self.s.ollama_url}/api/tags", timeout=5)
            r.raise_for_status()
        except httpx.HTTPError as e:
            return {"status": "unavailable", "detail": f"{type(e).__name__}: {e}"}
        models = {m["name"]: m for m in r.json().get("models", [])}
        wanted = self.s.model if ":" in self.s.model else self.s.model + ":latest"
        if wanted not in models:
            return {"status": "unavailable", "detail": f"server up, model {wanted} not pulled",
                    "models": sorted(models)}
        self._digest = models[wanted].get("digest")
        return {"status": "ready", "model": wanted, "digest": self._digest,
                "details": models[wanted].get("details", {})}

    def complete(self, role, system, prompt, schema: type[BaseModel], temperature=0.2, tag="", seed=None):
        """Ask for JSON matching `schema`; retry once with the validation error if it does not match."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        last_error = None
        for attempt in range(2):
            body = {
                "model": self.s.model, "messages": messages, "stream": False,
                "format": schema.model_json_schema(),
                "options": {"temperature": temperature, "seed": self.s.seed if seed is None else seed, "num_ctx": self.s.num_ctx,
                            "num_predict": self.s.num_predict},
            }
            started = time.time()
            try:
                r = httpx.post(f"{self.s.ollama_url}/api/chat", json=body, timeout=self.s.llm_timeout)
                r.raise_for_status()
            except httpx.HTTPError as e:
                raise LLMError(f"ollama request failed: {type(e).__name__}: {e}") from e
            data = r.json()
            content = data.get("message", {}).get("content", "")
            meta = {"provider": self.name, "fake": False, "model": data.get("model", self.s.model),
                    "digest": self._digest, "role": role, "temperature": temperature, "seed": self.s.seed if seed is None else seed,
                    "num_ctx": self.s.num_ctx, "num_predict": self.s.num_predict, "attempt": attempt + 1,
                    "seconds": round(time.time() - started, 1),
                    "prompt_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count")}
            self._log(role, tag, body, content, meta)
            try:
                return schema.model_validate_json(content), meta
            except ValidationError as e:
                last_error = e
                messages += [{"role": "assistant", "content": content},
                             {"role": "user", "content": f"That JSON did not match the schema: {e}. Reply again with valid JSON only."}]
        raise LLMError(f"{role}: model output did not match schema after retry: {last_error}")

    def _log(self, role, tag, body, content, meta):
        if not self.log_dir:
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{tag or 'call'}-{role}.json".replace("/", "_")
        (self.log_dir / name).write_text(json.dumps(
            {"meta": meta, "messages": body["messages"], "response": content}, indent=2), encoding="utf-8")


class FakeProvider:
    """Deterministic provider for unit tests ONLY. Never evidence that real model reasoning works."""
    name = "fake"
    fake = True

    def __init__(self, responder):
        # responder(role, prompt, schema) -> dict matching schema
        self.responder = responder
        self.calls = []

    def health(self):
        return {"status": "ready", "model": "FAKE-deterministic", "fake": True}

    def complete(self, role, system, prompt, schema, temperature=0.2, tag="", seed=None):
        self.calls.append({"role": role, "prompt": prompt, "tag": tag})
        out = schema.model_validate(self.responder(role, prompt, schema))
        return out, {"provider": "fake", "fake": True, "model": "FAKE-deterministic", "role": role}


def make_provider(settings, log_dir=None):
    if settings.provider == "ollama":
        return OllamaProvider(settings, log_dir)
    raise ValueError(f"unknown provider {settings.provider!r} (the fake provider is only constructed by tests)")
