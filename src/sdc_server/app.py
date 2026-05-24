import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from sdc_server.license_gate import LicenseError, verify_license
from sdc_server.routers import v1
from sdc_server.utils import OperationOutcomeException

logging.basicConfig(level=logging.INFO)

LOGGER = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("FHIR_SDC_LICENSE_SKIP") == "1":
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


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    LOGGER.exception("Unhandled exception")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


app.include_router(v1.router, prefix="/api/v1")
