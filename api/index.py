"""Vercel serverless entry point.

Exposes the Flask app from the project-root app module. Static files
under public/static are served by Vercel's CDN before rewrites apply.
The request-path restoration for Vercel's rewrite lives in app.py
(app.wsgi_app), so it applies however the runtime invokes the app.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402,F401
