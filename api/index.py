"""Vercel serverless entry point.

Exposes the Flask app from the project-root app module. All paths are
rewritten here by vercel.json; static files under public/static are
served by Vercel's CDN before the rewrite applies.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402,F401
