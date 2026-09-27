"""Vercel FastAPI entrypoint (zero-config looks for main.py:app)."""

from app.main import app

__all__ = ["app"]
