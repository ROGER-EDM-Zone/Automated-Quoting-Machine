"""Quote endpoints: notes, approval, draft reply, outcome."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import CurrentUser, get_ai, get_current_user, get_quote
from app.models import Flag, Quote
from app.schemas import (
    ApproveRequest,
    FlagOut,
    FlagResolveIn,
    NoteIn,
    OutcomeIn,
    QuoteNoteOut,
    QuoteOut,
)
from app.services.approval import (
    ApprovalBlocked,
    approve,
    mark_sent,
    record_outcome,
    revise,
    unresolved_blockers,
)
from app.services.flags import resolve_flag
from app.services.graph import GraphError, GraphNotConfigured, get_graph_client
from app.services.notes import NoteError, add_note
from app.services.reply import ReplyError, build_reply

logger = logging.getLogger(__name__)

router = APIRouter(tags=["quotes"])


@router.post("/quotes/{quote_id}/notes", response_model=QuoteNoteOut)
def create_note(
    body: NoteIn,
    quote: Quote = Depends(get_quote),
    db: Session = Depends(get_db),
    ai=Depends(get_ai),
    user: CurrentUser = Depends(get_current_user),
):
    """Add a note: the AI proposes an input change, the engine reprices."""
    try:
        note = add_note(
            db, quote.enquiry, quote, note_text=body.note_text, author=user.email, ai=ai
        )
    except NoteError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    db.refresh(note)
    return note


@router.post("/quotes/{quote_id}/approve", response_model=QuoteOut)
def approve_quote(
    body: ApproveRequest | None = None,
    quote: Quote = Depends(get_quote),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Approve. Refused while any `block` flag is unresolved."""
    body = body or ApproveRequest()
    if body.lead_time_days is not None:
        quote.lead_time_days = body.lead_time_days
    try:
        approve(db, quote.enquiry, quote, approved_by=user.email)
    except ApprovalBlocked as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=exc.as_dict()) from exc
    db.commit()
    db.refresh(quote)
    return quote


@router.get("/quotes/{quote_id}/blockers", response_model=list[FlagOut])
def get_blockers(
    quote: Quote = Depends(get_quote),
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    """What is standing between this quote and approval."""
    return unresolved_blockers(db, quote.enquiry, quote)


@router.post("/flags/{flag_id}/resolve", response_model=FlagOut)
def resolve(
    flag_id: int,
    body: FlagResolveIn | None = None,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    flag = db.get(Flag, flag_id)
    if flag is None:
        raise HTTPException(status_code=404, detail=f"Flag {flag_id} not found")
    resolve_flag(db, flag, resolved_by=user.email, note=(body.note if body else None))
    db.commit()
    db.refresh(flag)
    return flag


@router.post("/quotes/{quote_id}/draft-reply")
def draft_reply(
    quote: Quote = Depends(get_quote),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Create the reply as an Outlook draft. A human presses send.

    The email body is rendered from the stored quote, so what was approved is
    exactly what sends. When Graph is not configured the rendered draft is
    returned for copying by hand rather than failing outright.
    """
    enquiry = quote.enquiry
    try:
        reply = build_reply(db, enquiry, quote)
    except ReplyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if not enquiry.outlook_message_id:
        return {"draft_created": False, "reason": "no Outlook message to reply to", **reply.as_dict()}

    try:
        client = get_graph_client()
        result = client.create_draft_reply(
            message_id=enquiry.outlook_message_id,
            mailbox=client.settings.graph_quoting_mailbox,
            subject=reply.subject,
            body_html=reply.body_html,
        )
    except GraphNotConfigured as exc:
        logger.info("Graph not configured; returning the rendered draft instead")
        return {"draft_created": False, "reason": str(exc), **reply.as_dict()}
    except GraphError as exc:
        raise HTTPException(status_code=502, detail=f"Graph draft failed: {exc}") from exc

    quote.outlook_draft_id = result["draft_id"]
    db.commit()
    return {"draft_created": True, **result, **reply.as_dict()}


@router.post("/quotes/{quote_id}/mark-sent", response_model=QuoteOut)
def mark_quote_sent(
    quote: Quote = Depends(get_quote),
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    """Record that the draft was sent: sets sent_at, turnaround, and freezes.

    Called when the estimator confirms they pressed send (or by a Graph
    subscription on the mailbox's Sent Items). Nothing here sends the email.
    """
    try:
        mark_sent(db, quote.enquiry, quote)
    except ApprovalBlocked as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Only an approved quote can be marked as sent",
        ) from exc
    db.commit()
    db.refresh(quote)
    return quote


@router.post("/quotes/{quote_id}/outcome", response_model=QuoteOut)
def set_outcome(
    body: OutcomeIn,
    quote: Quote = Depends(get_quote),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Won/lost and actual production time — what calibrates future estimates."""
    record_outcome(
        db,
        quote.enquiry,
        quote,
        result=body.result.value,
        actual_production_mins=body.actual_production_mins,
        recorded_by=user.email,
        notes=body.notes,
    )
    db.commit()
    db.refresh(quote)
    return quote


@router.post("/quotes/{quote_id}/revise", response_model=QuoteOut)
def revise_quote(
    quote: Quote = Depends(get_quote),
    db: Session = Depends(get_db),
    _user: CurrentUser = Depends(get_current_user),
):
    """Start a new version after a quote has gone out."""
    try:
        revision = revise(db, quote.enquiry, quote)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    db.refresh(revision)
    return revision
