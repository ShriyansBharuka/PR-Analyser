import json
import os

from appdirs import user_cache_dir

RESULTS_FILE = "results.json"


def cache_dir():
    path = os.environ.get("LINKWATCH_CACHE_DIR") or user_cache_dir("linkwatch")
    os.makedirs(path, exist_ok=True)
    return path


def cache_file():
    return os.path.join(cache_dir(), RESULTS_FILE)


def save_results(results):
    path = cache_file()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    return path


def load_results():
    path = cache_file()
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
