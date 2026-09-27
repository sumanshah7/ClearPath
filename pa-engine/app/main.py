"""ClearPath PA engine."""

from __future__ import annotations

import logging
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import boundary, fhir, health, pa, policies, reports, review, uploads
from app.config import PA_ENGINE_ROOT, ROOT, settings
from app.errors import ApiError
from app.repository import get_repo

log = logging.getLogger("clearpath")


def _prepare_vercel_writable_paths() -> None:
    """SQLite and uploads need a writable filesystem; Vercel only allows /tmp."""
    if os.environ.get("VERCEL") != "1":
        return
    bundled = Path(str(PA_ENGINE_ROOT / "data" / "clearpath.db"))
    if not bundled.exists():
        alt = ROOT / "data" / "clearpath.db"
        if alt.exists():
            bundled = alt
    # Ignore env DATABASE_PATH pointing at /var/task until after we stage into /tmp.
    tmp_db = Path("/tmp/clearpath.db")
    seed_marker = Path("/tmp/clearpath.db.seed-bytes")
    if bundled.exists():
        seed_bytes = str(bundled.stat().st_size)
        need_copy = (not tmp_db.exists()) or (not seed_marker.exists()) or seed_marker.read_text() != seed_bytes
        if need_copy:
            shutil.copy2(bundled, tmp_db)
            seed_marker.write_text(seed_bytes)
            log.warning("Staged SQLite seed to %s (%s bytes)", tmp_db, tmp_db.stat().st_size)
    os.environ["DATABASE_PATH"] = str(tmp_db if tmp_db.exists() else bundled)
    storage = Path("/tmp/clearpath-storage")
    storage.mkdir(parents=True, exist_ok=True)
    os.environ["STORAGE_DIR"] = str(storage)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _prepare_vercel_writable_paths()
    # Orphaned ingestion_runs / status=ingesting after prior process kill/reload.
    try:
        n = get_repo().reclaim_stale_ingestion()
        if n:
            log.warning("Reclaimed %s stale ingestion run(s) on startup", n)
    except Exception:
        pass
    yield


app = FastAPI(title="ClearPath PA Engine", version="4", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(health.router)
app.include_router(policies.router)
app.include_router(review.router)
app.include_router(reports.router)
app.include_router(pa.router)
app.include_router(fhir.router)
app.include_router(boundary.router)
app.include_router(uploads.router)


@app.exception_handler(ApiError)
def api_error(_: object, exc: ApiError):
    body = {"error": {"code": exc.code, "message": exc.message, **exc.extra}}
    return JSONResponse(status_code=exc.status, content=body)
