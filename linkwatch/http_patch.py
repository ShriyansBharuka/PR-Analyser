import requests

USER_AGENT = "linkwatch/1.0"

_original_request = requests.Session.request


def _request(self, method, url, **kwargs):
    headers = dict(kwargs.pop("headers", None) or {})
    headers["User-Agent"] = USER_AGENT
    return _original_request(self, method, url, headers=headers, **kwargs)


requests.Session.request = _request
