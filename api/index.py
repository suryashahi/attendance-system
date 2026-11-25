"""Vercel serverless entry point.

Exposes the Flask app from the project-root app module. Static files
under public/static are served by Vercel's CDN before rewrites apply.

vercel.json rewrites every path here as /api/index?__path=/<original>,
because the Python runtime reports the function path (/api/index) as
PATH_INFO rather than the original request path, which would make
every Flask route 404. The wrapper below restores the original path
from __path (and removes it from the query string) before Flask
routes the request.
"""

import os
import sys
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app as _flask_app  # noqa: E402

_FUNCTION_PREFIXES = ("/api/index.py", "/api/index")


def _restore_path(environ):
    pairs = parse_qsl(environ.get("QUERY_STRING", ""), keep_blank_values=True)
    path = None
    kept = []
    for key, value in pairs:
        if key == "__path" and path is None:
            path = value
        else:
            kept.append((key, value))
    if path is not None:
        environ["QUERY_STRING"] = urlencode(kept)
        # The rewrite capture may arrive with no leading slash or a
        # doubled one; normalize to exactly one.
        path = "/" + path.lstrip("/")
        for prefix in _FUNCTION_PREFIXES:
            if path == prefix:
                path = "/"
                break
            if path.startswith(prefix + "/"):
                path = path[len(prefix):] or "/"
                break
        return path

    header = environ.get("HTTP_X_VERCEL_ORIGINAL_PATH")
    if header:
        header_path = unquote(urlsplit(header).path)
        if header_path and header_path not in _FUNCTION_PREFIXES:
            return header_path

    current = environ.get("PATH_INFO") or "/"
    for prefix in _FUNCTION_PREFIXES:
        if current == prefix:
            uri = environ.get("REQUEST_URI") or environ.get("RAW_URI") or ""
            uri_path = unquote(urlsplit(uri).path)
            if uri_path and uri_path not in _FUNCTION_PREFIXES:
                return uri_path
            return "/"
        if current.startswith(prefix + "/"):
            return current[len(prefix):] or "/"
    return current


def app(environ, start_response):
    environ["PATH_INFO"] = _restore_path(environ)
    environ["SCRIPT_NAME"] = ""
    return _flask_app(environ, start_response)
