"""FastAPI application.

One service, modular internally (spec section 1). The pipeline stages are
separate modules under `services/` and separate endpoints, so a stage can be
re-run on its own — which is what makes extraction measurable against real
drawings before any UI is built around it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import poller
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

    poll_task: asyncio.Task | None = None
    if poller.should_run(settings):
        poll_task = asyncio.create_task(poller.run(settings))
    elif settings.mailbox_poll_enabled:
        logger.info(
            "Mailbox poll is on but Graph is not configured, so nothing will "
            "be checked. Set the Graph settings in .env to switch it on."
        )

    try:
        yield
    finally:
        if poll_task is not None:
            poll_task.cancel()
            # Wait for it to actually stop, so a poll mid-flight finishes its
            # transaction rather than being torn down half-committed.
            with suppress(asyncio.CancelledError):
                await poll_task


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

# Everything the API answers lives under /api. It has to: the front end
# routes in the browser, and its addresses are the same words — /queue,
# /enquiry/12, /admin/rates. Sharing one port without this prefix means a
# hard refresh on /queue returns the JSON list instead of the screen, which
# is exactly what it did before the prefix was added.
API_PREFIX = "/api"

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
    app.include_router(router, prefix=API_PREFIX)


@app.get("/api/health", tags=["ops"])
def health() -> dict:
    return {
        "status": "ok",
        "environment": settings.environment,
        "auth_required": settings.auth_required,
        "ai_configured": bool(settings.anthropic_api_key),
        "graph_configured": bool(settings.graph_client_id and settings.graph_quoting_mailbox),
        "mailbox_poll": poller.should_run(settings),
    }


# --------------------------------------------------------------------------
# The screens
# --------------------------------------------------------------------------
# Serving the built front end from the same process is what turns this from
# two things to run into one. On a shop-floor PC that matters more than the
# tidiness of separating them: one shortcut, one address, one thing that can
# be off. In development the Vite server does this instead and this directory
# does not exist, which is why it is optional rather than required.
FRONTEND_DIST = Path(__file__).resolve().parents[1] / "web"


def _mount_frontend() -> None:
    index = FRONTEND_DIST / "index.html"
    if not index.exists():
        logger.info(
            "No built front end at %s — API only. Run `npm run build` in "
            "frontend/ and copy dist/ here to serve the screens too.",
            FRONTEND_DIST,
        )
        return

    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIST / "assets"),
        name="assets",
    )

    @app.get("/{path:path}", include_in_schema=False)
    def serve_screens(path: str) -> FileResponse:
        """Hand any unmatched address to the front end.

        It routes in the browser, so /queue and /enquiry/12 are its addresses,
        not the server's. Registered last so every real endpoint above wins
        first — otherwise this would swallow the entire API.
        """
        candidate = (FRONTEND_DIST / path).resolve()
        # Only ever serve files from inside the build directory: a path like
        # ../../.env must not become a download.
        if path and candidate.is_file() and FRONTEND_DIST.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index)

    logger.info("Serving the screens from %s", FRONTEND_DIST)


_mount_frontend()
