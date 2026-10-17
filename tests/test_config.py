import os

from linkwatch.config import Settings, Site, load_settings

SITES_FILE = os.path.join(os.path.dirname(__file__), os.pardir, "sites.toml")


def test_sites_file_loads():
    settings = load_settings(SITES_FILE)
    assert isinstance(settings, Settings)
    assert len(settings.sites) > 0
    assert all(isinstance(s, Site) for s in settings.sites)
    assert settings.output == "status.html"
