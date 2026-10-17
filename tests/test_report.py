from click.testing import CliRunner

from linkwatch import cache
from linkwatch.cli import cli

RESULTS = [
    {"name": "alpha", "url": "http://a.test", "up": True, "status": 200, "reason": "OK",
     "title": "Alpha", "elapsed_ms": 12, "last_modified": None,
     "checked_at": "2022-03-01T10:00:00+00:00"},
    {"name": "beta", "url": "http://b.test", "up": False, "status": None,
     "reason": "connection error", "title": "", "elapsed_ms": None,
     "last_modified": None, "checked_at": "2022-03-01T10:00:00+00:00"},
]


def test_report_renders_all_sites(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKWATCH_CACHE_DIR", str(tmp_path))
    cache.save_results(RESULTS)
    output = tmp_path / "status.html"
    result = CliRunner().invoke(cli, ["report", "-o", str(output)], catch_exceptions=False)
    assert result.exit_code == 0
    page = output.read_text(encoding="utf-8")
    for r in RESULTS:
        assert r["name"] in page
