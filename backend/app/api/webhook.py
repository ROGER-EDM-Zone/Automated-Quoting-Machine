"""Graph notification receiver (spec stage 1).

Graph's handshake: a new subscription is validated by echoing back a token in
plain text within a few seconds. Notifications then arrive as small JSON
envelopes; the message itself has to be fetched.
"""

from __future__ import annotations

import logging

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
)
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import SessionLocal, get_db
from app.deps import CurrentUser, get_current_user
from app.services.graph import GraphError, GraphNotConfigured, get_graph_client
from app.services.intake import ingest_message, poll_mailbox

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


@router.post("/intake/poll")
def poll_now(
    limit: int = Query(default=25, ge=1, le=100),
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    """Check the mailbox for tagged mail we have not seen yet.

    What the workspace's "Check for new enquiries" button calls. Also the way
    to run intake before there is a public address for Graph to push to.
    """
    try:
        result = poll_mailbox(db, limit=limit)
    except GraphNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except GraphError as exc:
        raise HTTPException(status_code=502, detail=f"Mailbox check failed: {exc}") from exc

    db.commit()
    return {
        "checked": result.checked,
        "new_enquiries": result.ingested,
        "already_known": result.already_known,
        "failed": result.failed,
    }


@router.get("/intake/connection")
def connection_status(
    settings: Settings = Depends(get_settings),
    _user: CurrentUser = Depends(get_current_user),
):
    """Is the mailbox connection actually working, and if not, what is wrong?

    Deliberately specific: "the category does not exist in this mailbox" is a
    fixable sentence, where "no enquiries appeared" is a week of confusion.
    """
    report: dict[str, object] = {
        "configured": False,
        "mailbox": settings.graph_quoting_mailbox,
        "category": settings.graph_rfq_category,
        "connected": False,
        "problems": [],
    }
    problems: list[str] = report["problems"]  # type: ignore[assignment]

    try:
        client = get_graph_client()
        client.token()
        report["configured"] = True
    except GraphNotConfigured as exc:
        problems.append(str(exc))
        return report
    except GraphError as exc:
        report["configured"] = True
        problems.append(f"Could not get a token: {exc}")
        return report

    try:
        mailbox = client.check_mailbox()
        report["mailbox_name"] = mailbox.get("displayName")
        report["connected"] = True
    except GraphError as exc:
        problems.append(f"Signed in, but cannot read {settings.graph_quoting_mailbox}: {exc}")
        return report

    try:
        categories = client.list_categories()
        report["categories"] = categories
        if settings.graph_rfq_category and settings.graph_rfq_category not in categories:
            problems.append(
                f"No category named '{settings.graph_rfq_category}' exists in this "
                "mailbox. Tagged mail will never be picked up until it does — "
                "create it in Outlook, or change AQM_GRAPH_RFQ_CATEGORY to match "
                "one that exists."
            )
    except GraphError as exc:
        problems.append(f"Could not read the mailbox categories: {exc}")

    try:
        tagged = client.list_tagged_messages(limit=5)
        report["tagged_messages_waiting"] = len(tagged)
    except GraphError as exc:
        problems.append(f"Could not list tagged mail: {exc}")

    return report


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
