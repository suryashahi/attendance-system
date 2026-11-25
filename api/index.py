"""Vercel serverless entry point.

Exposes the Flask app from the project-root app module. All paths are
rewritten here by vercel.json; static files under public/static are
served by Vercel's CDN before the rewrite applies.

The rewrite invokes this function as /api/index, and the Python
runtime may report that function path as PATH_INFO instead of the
original request path, which would make every Flask route 404. The
wrapper below restores the original path before Flask routes it.
"""

import os
import sys
from urllib.parse import unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app as _flask_app  # noqa: E402

_FUNCTION_PREFIXES = ("/api/index.py", "/api/index")


def _original_path(environ):
    header = environ.get("HTTP_X_VERCEL_ORIGINAL_PATH")
    if header:
        return unquote(urlsplit(header).path) or "/"
    path = environ.get("PATH_INFO") or "/"
    for prefix in _FUNCTION_PREFIXES:
        if path == prefix:
            uri = environ.get("REQUEST_URI") or environ.get("RAW_URI") or ""
            uri_path = unquote(urlsplit(uri).path)
            if uri_path and uri_path not in _FUNCTION_PREFIXES:
                return uri_path
            return "/"
        if path.startswith(prefix + "/"):
            return path[len(prefix):] or "/"
    return path


def app(environ, start_response):
    environ["PATH_INFO"] = _original_path(environ)
    environ["SCRIPT_NAME"] = ""
    return _flask_app(environ, start_response)
