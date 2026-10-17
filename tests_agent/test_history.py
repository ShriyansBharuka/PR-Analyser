"""Decision log: references must be real, secrets are scrubbed, failures expire from 'skip' to 'retest'."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from resilience_agent import history
from resilience_agent.verification import scrub


def entry(settings, ref, at=None, result="failed"):
    return history.DecisionLog(
        entry_id="repair-1", kind="repair", subject="RA-x", status="no_verified_solution", verdict="NO_VERIFIED_SOLUTION",
        created_at=at or history.now(),
        attempts=[history.Attempt(patch_summary="pin x==2", diff_sha="abc", result=result, reason="2 tests fail",
                                  label="VERIFIED", ref=ref, at=at or history.now())])


def test_reason_must_reference_existing_artifact(settings, tmp_path):
    with pytest.raises(ValueError, match="missing artifacts"):
        history.save(settings, entry(settings, str(tmp_path / "nope.json")))
    with pytest.raises(ValueError):
        history.save(settings, entry(settings, "approval:999"), settings.db())
    log = tmp_path / "result.json"
    log.write_text("{}")
    path = history.save(settings, entry(settings, str(log)))
    assert path.exists() and path.with_suffix(".md").exists()


def test_human_approval_is_a_valid_reference(settings):
    db = settings.db()
    db.record_approval("RA-x", "issue", "approve", "ok")
    e = entry(settings, "approval:1")
    history.check_refs(e, db)


def test_secrets_are_scrubbed_before_saving(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("MY_API_TOKEN", "super-secret-value-123")
    log = tmp_path / "r.json"
    log.write_text("{}")
    e = entry(settings, str(log))
    e.attempts[0].reason = "failed with super-secret-value-123 and ghp_" + "a" * 30 + " password=hunter2"
    text = history.save(settings, e).read_text(encoding="utf-8")
    assert "super-secret" not in text and "ghp_" not in text and "hunter2" not in text
    assert scrub("SESSIONNAME Console") == "SESSIONNAME Console"  # short env values are not treated as secrets


def test_failures_skip_when_fresh_and_retest_when_expired(settings, tmp_path):
    log = tmp_path / "r.json"
    log.write_text("{}")
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat(timespec="seconds")
    history.save(settings, entry(settings, str(log)))
    e = entry(settings, str(log), at=old)
    e.entry_id = "repair-0"
    history.save(settings, e)
    statuses = sorted(f["status"] for f in history.prior_failures(settings, "RA-x", max_age_days=30))
    assert statuses == ["retest", "skip"]
    note = history.prompt_note(history.prior_failures(settings, "RA-x", max_age_days=30))
    assert "do not repeat" in note and "retrying is allowed" in note
    cand = {"diff": "same"}
    assert history.is_fresh_failure(cand, [{"status": "skip", "diff_sha": history.diff_sha("same"), "at": "x",
                                           "entry": "e"}])
    assert not history.is_fresh_failure(cand, [{"status": "retest", "diff_sha": history.diff_sha("same")}])


def test_ai_text_only_in_ai_summary(settings, tmp_path):
    log = tmp_path / "r.json"
    log.write_text("{}")
    e = entry(settings, str(log))
    e.ai_summary = "the model thinks it is fine"
    data = json.loads(history.save(settings, e).read_text(encoding="utf-8"))
    assert data["ai_summary"] == "the model thinks it is fine"
    assert "AI summary (AI, not evidence)" in history.render_md(e)


def test_scrub_keeps_refs_resolvable(settings, tmp_path, monkeypatch):
    sub = tmp_path / "abcdef0123456789-session"
    sub.mkdir()
    log = sub / "r.json"
    log.write_text("{}")
    monkeypatch.setenv("SOME_SESSION_ID", "abcdef0123456789-session")  # an identifier, not a secret
    monkeypatch.setenv("SOME_API_TOKEN", str(sub.name))  # even a secret-named var must not break a ref
    data = json.loads(history.save(settings, entry(settings, str(log))).read_text(encoding="utf-8"))
    assert data["attempts"][0]["ref"] == str(log)
