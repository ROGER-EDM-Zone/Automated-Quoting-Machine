"""FastAPI application.

One service, modular internally (spec section 1). The pipeline stages are
separate modules under `services/` and separate endpoints, so a stage can be
re-run on its own — which is what makes extraction measurable against real
drawings before any UI is built around it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import admin, customers, enquiries, parts, quotes, reports, search, webhook
from app.config import get_settings
from app.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    if settings.environment == "development":
        # Alembic owns the schema elsewhere; this keeps local setup to one step.
        init_db()
    if not settings.auth_required:
        logger.warning(
            "AQM_AUTH_REQUIRED is false — approvals will be recorded against "
            "the X-User-Email header. Do not run this way in production."
        )
    if not settings.anthropic_api_key:
        logger.warning(
            "No Anthropic API key set — extraction and classification will "
            "fail. The deterministic pricing engine works regardless."
        )
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Quoting Automation",
    version="0.1.0",
    description=(
        "Inbound RFQ email to a structured, priced, flagged draft quote for "
        "human review. Nothing sends without a person approving it."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for router in (
    webhook.router,
    enquiries.router,
    parts.router,
    quotes.router,
    search.router,
    customers.router,
    admin.router,
    reports.router,
):
    app.include_router(router)


@app.get("/health", tags=["ops"])
def health() -> dict:
    return {
        "status": "ok",
        "environment": settings.environment,
        "auth_required": settings.auth_required,
        "ai_configured": bool(settings.anthropic_api_key),
        "graph_configured": bool(settings.graph_client_id and settings.graph_quoting_mailbox),
    }
