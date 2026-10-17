import os

from linkwatch import cache


def test_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKWATCH_CACHE_DIR", str(tmp_path))
    results = [{"name": "alpha", "url": "http://a.test", "up": True, "status": 200}]
    path = cache.save_results(results)
    assert os.path.dirname(path) == str(tmp_path)
    assert os.path.exists(path)
    assert cache.load_results() == results
