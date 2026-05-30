import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from sdc_server._build_flags import ALLOW_LICENSE_SKIP
from sdc_server.integrity_check import (
    IntegrityError,
    manifest_present,
    verify_integrity,
)
from sdc_server.license_gate import LicenseError, bypass_requested, verify_license
from sdc_server.routers import v1
from sdc_server.utils import OperationOutcomeException

logging.basicConfig(level=logging.INFO)

LOGGER = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Integrity check first: in a release build the manifest is baked in by
    # the Dockerfile and must verify. In a dev build (ALLOW_LICENSE_SKIP=True)
    # the source tree has no /app/integrity/, so skip with a warning.
    if not manifest_present():
        if ALLOW_LICENSE_SKIP:
            LOGGER.warning(
                "Integrity manifest not found — skipping (dev build only)"
            )
        else:
            raise IntegrityError(
                "integrity manifest missing in release build — refusing to start"
            )
    else:
        try:
            verify_integrity()
        except IntegrityError as exc:
            LOGGER.error("Integrity check failed: %s", exc)
            raise
        LOGGER.info("Integrity manifest verified")

    if bypass_requested():
        LOGGER.warning("FHIR_SDC_LICENSE_SKIP=1 — license verification bypassed")
    else:
        try:
            claims = verify_license()
        except LicenseError as exc:
            LOGGER.error("License check failed: %s", exc)
            raise
        sub = claims.get("sub", "?")
        exp = claims.get("exp", 0)
        remaining_days = max(0.0, (exp - time.time()) / 86400)
        LOGGER.info(
            "License valid for sub=%s (%.1f days remaining)", sub, remaining_days
        )
    yield


app = FastAPI(title="Tiro SDC Extract service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(OperationOutcomeException)
async def operation_outcome_exception_handler(
    request: Request, exc: OperationOutcomeException
):
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.detail,
        media_type="application/fhir+json",
    )


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
):
    """Body that FastAPI/Pydantic couldn't parse (e.g. malformed JSON) before the
    operation model's own validator runs. Return a FHIR OperationOutcome rather
    than FastAPI's default `{"detail": ...}` so the error contract stays FHIR."""
    return JSONResponse(
        status_code=400,
        media_type="application/fhir+json",
        content={
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": "structure",
                    "diagnostics": err.get("msg", "Invalid request body"),
                }
                for err in exc.errors()
            ],
        },
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    LOGGER.exception("Unhandled exception")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


app.include_router(v1.router, prefix="/api/v1")
