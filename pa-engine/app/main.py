"""ClearPath PA engine."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import boundary, fhir, health, pa, policies, reports, review, uploads
from app.config import settings
from app.errors import ApiError
from app.repository import get_repo


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Orphaned ingestion_runs / status=ingesting after prior process kill/reload.
    try:
        n = get_repo().reclaim_stale_ingestion()
        if n:
            import logging

            logging.getLogger("clearpath").warning("Reclaimed %s stale ingestion run(s) on startup", n)
    except Exception:
        pass
    yield


app = FastAPI(title="ClearPath PA Engine", version="4", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
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
