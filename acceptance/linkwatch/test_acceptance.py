"""Independent acceptance criteria for LinkWatch, owned by the verifier.

Repair candidates are not allowed to modify this folder. These tests pin the behaviour that a
dependency migration must preserve. They run against the source tree given in RA_TARGET_SRC
(the current directory when run by the agent).
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SRC = Path(os.environ.get("RA_TARGET_SRC", os.getcwd())).resolve()
sys.path.insert(0, str(SRC))

from linkwatch import cache, checker, config  # noqa: E402


def test_shipped_sites_file_loads():
    settings = config.load_settings(str(SRC / "sites.toml"))
    assert settings.title and len(settings.sites) > 0
    assert all(s.url.startswith(("http://", "https://")) for s in settings.sites)


def test_whitespace_is_stripped_from_site_fields():
    site = config.Site(name="  Example  ", url="  https://example.com  ")
    assert site.name == "Example"
    assert site.url == "https://example.com"


@pytest.mark.parametrize("bad", [
    {"name": "", "url": "https://example.com"},
    {"name": "x", "url": "ftp://example.com"},
    {"name": "x", "url": "example.com"},
])
def test_invalid_sites_are_rejected(bad):
    with pytest.raises(ValueError):
        config.Site(**bad)


def test_unknown_top_level_keys_are_rejected():
    with pytest.raises(ValueError):
        config.Settings(title="t", unexpected="x")


def test_settings_defaults():
    s = config.Settings()
    assert s.title == "linkwatch" and s.output == "status.html" and s.sites == []


def test_load_settings_from_toml(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text('title = "T"\n[[sites]]\nname = "A"\nurl = "https://a.test"\n', encoding="utf-8")
    s = config.load_settings(str(p))
    assert s.title == "T" and s.sites[0].name == "A"


def test_invalid_toml_file_is_rejected(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text('title = "T"\n[[sites]]\nname = "A"\nurl = "nope"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        config.load_settings(str(p))


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b"<html><head><title>Accept</title></head></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_http_check_against_local_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = "http://127.0.0.1:%d/" % httpd.server_address[1]
        result = checker.check_site(config.Site(name="local", url=url))
        assert result["up"] is True and result["status"] == 200 and result["title"] == "Accept"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cache_round_trip_and_report(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKWATCH_CACHE_DIR", str(tmp_path))
    rows = [{"name": "alpha", "url": "http://a.test", "up": True, "status": 200, "reason": "OK",
             "title": "Alpha", "elapsed_ms": 1, "last_modified": None, "checked_at": "2026-01-01T00:00:00+00:00"}]
    from linkwatch.report import render_report  # imported here so a broken report stack fails only this test

    cache.save_results(rows)
    assert cache.load_results() == rows
    out = render_report(cache.load_results(), output=str(tmp_path / "status.html"), title="T")
    page = Path(out).read_text(encoding="utf-8")
    assert "alpha" in page and "T" in page
    assert json.loads(Path(cache.cache_file()).read_text(encoding="utf-8")) == rows
