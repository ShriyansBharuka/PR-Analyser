import time
from datetime import datetime, timezone

import certifi
import requests
from requests import Session as S

from .dates import parse_last_modified
from .encoding import page_title


def fetch(url):
    session = S()
    try:
        return session.get(url, verify=certifi.where())
    finally:
        session.close()


def check_site(site):
    result = {
        "name": site.name,
        "url": site.url,
        "up": False,
        "status": None,
        "reason": "",
        "title": "",
        "elapsed_ms": None,
        "last_modified": None,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    started = time.monotonic()
    try:
        resp = fetch(site.url)
    except requests.exceptions.ConnectionError:
        result["reason"] = "connection error"
        return result
    except requests.exceptions.Timeout:
        result["reason"] = "timed out"
        return result
    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    result["status"] = resp.status_code
    result["up"] = resp.ok
    result["reason"] = resp.reason or ""
    result["title"] = page_title(resp)
    modified = parse_last_modified(resp.headers.get("Last-Modified"))
    if modified:
        result["last_modified"] = modified.isoformat()
    return result


def check_all(sites):
    for site in sites:
        yield check_site(site)
