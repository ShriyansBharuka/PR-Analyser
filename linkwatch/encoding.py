import html
import re

import chardet
from requests.utils import _parse_content_type_header

TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def detect_encoding(resp):
    _, params = _parse_content_type_header(resp.headers.get("Content-Type", ""))
    charset = params.get("charset")
    if charset and charset is not True:
        return charset
    guess = chardet.detect(resp.content)
    return guess["encoding"] or "utf-8"


def page_title(resp):
    if not resp.content:
        return ""
    text = resp.content.decode(detect_encoding(resp), errors="replace")
    match = TITLE_RE.search(text)
    if not match:
        return ""
    return html.unescape(" ".join(match.group(1).split()))
