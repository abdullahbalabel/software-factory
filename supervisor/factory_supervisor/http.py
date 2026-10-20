"""Tiny JSON-over-HTTP helper on the standard library (the supervisor has no third-party HTTP dependency)."""
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class HttpError(Exception):
    def __init__(self, status, body, url):
        super().__init__(f"HTTP {status} for {url}: {body[:300]}")
        self.status = status
        self.body = body


def call(method, url, headers=None, json_body=None, form=None, timeout=60) -> Any:
    data = None
    hdrs = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode()
        hdrs["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, e.read().decode("utf-8", "replace"), url) from None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def fetch_bytes(url, max_bytes, headers=None, timeout=120):
    """GET a body as bytes; None when it is larger than max_bytes (never reads more than max_bytes + 1)."""
    req = urllib.request.Request(url, headers=dict(headers or {}), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, e.read().decode("utf-8", "replace"), url) from None
    return None if len(data) > max_bytes else data
