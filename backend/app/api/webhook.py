"""Graph notification receiver (spec stage 1).

Graph's handshake: a new subscription is validated by echoing back a token in
plain text within a few seconds. Notifications then arrive as small JSON
envelopes; the message itself has to be fetched.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import SessionLocal, get_db
from app.deps import CurrentUser, get_current_user
from app.services.graph import GraphError, GraphNotConfigured, get_graph_client
from app.services.intake import ingest_message

logger = logging.getLogger(__name__)

router = APIRouter(tags=["intake"])


@router.post("/webhook/outlook")
async def outlook_webhook(
    request: Request,
    background: BackgroundTasks,
    validation_token: str | None = Query(default=None, alias="validationToken"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Receive Graph notifications for the quoting mailbox."""
    # Subscription handshake: echo the token as plain text, nothing else.
    if validation_token:
        return Response(content=validation_token, media_type="text/plain")

    payload = await request.json()
    notifications = payload.get("value", [])
    expected_state = settings.graph_webhook_client_state

    accepted: list[str] = []
    for notification in notifications:
        if expected_state and notification.get("clientState") != expected_state:
            # Anyone can POST to this URL; the client state is what proves the
            # notification came from our subscription.
            logger.warning("Rejected notification with bad clientState")
            continue
        message_id = (notification.get("resourceData") or {}).get("id")
        if not message_id:
            continue
        accepted.append(message_id)

    # Graph expects a 202 within 3 seconds, so the fetch happens afterwards.
    for message_id in accepted:
        background.add_task(_ingest_in_background, message_id)

    return Response(status_code=202, content=None)


def _ingest_in_background(message_id: str) -> None:
    """Fetch and persist one message. Never raises into the request."""
    db = SessionLocal()
    try:
        client = get_graph_client()
        message = client.get_message(message_id)
        settings = get_settings()
        if settings.graph_rfq_category and settings.graph_rfq_category not in message.categories:
            logger.info("Message %s is not tagged for quoting; ignored", message_id)
            return
        result = ingest_message(db, message, require_category=settings.graph_rfq_category)
        db.commit()
        logger.info("Enquiry %s created from message %s", result.enquiry.id, message_id)
    except (GraphNotConfigured, GraphError):
        logger.exception("Could not ingest message %s", message_id)
        db.rollback()
    except Exception:
        logger.exception("Unexpected failure ingesting message %s", message_id)
        db.rollback()
    finally:
        db.close()


@router.post("/webhook/subscription")
def create_subscription(
    notification_url: str = Query(...),
    _user: CurrentUser = Depends(get_current_user),
):
    """Create the Graph mail subscription. Renew before it expires."""
    try:
        return get_graph_client().create_subscription(notification_url)
    except GraphNotConfigured as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/webhook/subscription/{subscription_id}/renew")
def renew_subscription(subscription_id: str, _user: CurrentUser = Depends(get_current_user)):
    try:
        return get_graph_client().renew_subscription(subscription_id)
    except GraphNotConfigured as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail=str(exc)) from exc
