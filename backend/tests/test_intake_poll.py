"""Tests for mailbox polling and the connection check.

Graph is stubbed — these cover the behaviour that matters when a real mailbox
is connected: not ingesting the same message twice, not stopping a sweep
because one message is broken, and telling someone precisely what is
misconfigured instead of failing silently.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.enums import AttachmentKind, EnquiryStatus
from app.models import Enquiry
from app.services.graph import GraphAttachment, GraphError, GraphMessage
from app.services.intake import classify_attachment, ingest_message, poll_mailbox
from app.services.storage import LocalStorage


class StubGraph:
    """Stands in for GraphClient. Records what was asked of it."""

    def __init__(self, messages: list[GraphMessage], broken: set[str] | None = None):
        self._messages = {m.message_id: m for m in messages}
        self._broken = broken or set()
        self.categories = ["RFQ", "Quoted"]
        self.listed = 0
        self.fetched: list[str] = []

    def list_tagged_messages(self, *, category=None, limit=25, since=None):
        self.listed += 1
        return [
            {
                "id": m.message_id,
                "subject": m.subject,
                "hasAttachments": bool(m.attachments),
                "from": {"emailAddress": {"address": m.sender_email}},
            }
            for m in list(self._messages.values())[:limit]
        ]

    def get_message(self, message_id: str) -> GraphMessage:
        self.fetched.append(message_id)
        if message_id in self._broken:
            raise GraphError(f"attachment fetch failed for {message_id}")
        return self._messages[message_id]

    def list_categories(self):
        return self.categories

    def check_mailbox(self):
        return {"displayName": "Quotes", "mail": "quotes@example.com"}


def message(msg_id: str, subject: str, *, with_drawing: bool = True) -> GraphMessage:
    attachments = []
    if with_drawing:
        attachments.append(
            GraphAttachment(
                filename=f"{msg_id}.pdf",
                content_type="application/pdf",
                content_bytes=f"%PDF-1.4 {msg_id}".encode(),
            )
        )
    return GraphMessage(
        message_id=msg_id,
        subject=subject,
        body_text="Please quote 4 off.",
        sender_email="buyer@bracken-eng.example",
        sender_name="Buyer",
        received_at=datetime.now(UTC),
        categories=["RFQ"],
        attachments=attachments,
    )


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path / "blobs")


def test_polling_ingests_tagged_mail(db, customer, storage):
    client = StubGraph([message("m1", "RFQ 4471"), message("m2", "RFQ 9000")])
    result = poll_mailbox(db, client=client, storage=storage)
    db.commit()

    assert result.checked == 2
    assert result.new_count == 2
    assert result.already_known == 0
    assert db.query(Enquiry).count() == 2
    # The customer was matched from the sender's domain.
    assert all(e.customer_id == customer.id for e in db.query(Enquiry).all())


def test_polling_twice_does_not_duplicate(db, customer, storage):
    client = StubGraph([message("m1", "RFQ 4471")])
    poll_mailbox(db, client=client, storage=storage)
    db.commit()

    second = poll_mailbox(db, client=client, storage=storage)
    db.commit()

    assert second.new_count == 0
    assert second.already_known == 1
    assert db.query(Enquiry).count() == 1


def test_a_known_message_is_not_even_fetched(db, customer, storage):
    """Skipping early keeps a poll cheap when the mailbox is mostly old mail."""
    client = StubGraph([message("m1", "RFQ 4471")])
    poll_mailbox(db, client=client, storage=storage)
    db.commit()
    client.fetched.clear()

    poll_mailbox(db, client=client, storage=storage)
    assert client.fetched == [], "an already-ingested message should not be re-downloaded"


def test_one_broken_message_does_not_stop_the_sweep(db, customer, storage):
    client = StubGraph(
        [message("m1", "RFQ 4471"), message("bad", "RFQ broken"), message("m3", "RFQ 9000")],
        broken={"bad"},
    )
    result = poll_mailbox(db, client=client, storage=storage)
    db.commit()

    assert result.new_count == 2
    assert len(result.failed) == 1
    assert "broken" in result.failed[0]
    assert db.query(Enquiry).count() == 2


def test_mail_with_no_drawing_is_kept_but_flagged(db, customer, storage):
    client = StubGraph([message("m1", "Just a question", with_drawing=False)])
    poll_mailbox(db, client=client, storage=storage)
    db.commit()

    enquiry = db.query(Enquiry).one()
    assert enquiry.status == EnquiryStatus.NEEDS_ATTENTION.value

    from app.models import Flag

    flags = db.query(Flag).filter(Flag.enquiry_id == enquiry.id).all()
    assert any("No drawing" in f.message for f in flags)


def test_ingest_is_idempotent_on_the_message_id(db, customer, storage):
    """Graph re-delivers notifications; a repeat must not create a second enquiry."""
    msg = message("m1", "RFQ 4471")
    first = ingest_message(db, msg, storage=storage)
    db.commit()
    second = ingest_message(db, msg, storage=storage)
    db.commit()

    assert second.created is False
    assert second.enquiry.id == first.enquiry.id
    assert db.query(Enquiry).count() == 1


def test_untagged_mail_is_refused(db, storage):
    msg = message("m1", "Not an RFQ")
    msg.categories = ["Newsletter"]
    with pytest.raises(ValueError, match="not tagged"):
        ingest_message(db, msg, storage=storage, require_category="RFQ")


def test_an_inline_signature_image_is_not_taken_for_a_drawing():
    """Otherwise every email footer becomes a part to quote."""
    assert classify_attachment("image003.png", "image/png", 4_000) == AttachmentKind.OTHER.value
    assert classify_attachment("logo.png", "image/png", 90_000) == AttachmentKind.OTHER.value
    assert (
        classify_attachment("4471.pdf", "application/pdf", 200_000) == AttachmentKind.DRAWING.value
    )
    # A large photo of a drawing is a drawing.
    assert classify_attachment("scan.jpg", "image/jpeg", 900_000) == AttachmentKind.DRAWING.value


def test_step_files_are_stored_but_marked_unread():
    """3D reading is deferred; recognising the kind now keeps it a later feature."""
    assert classify_attachment("model.STEP", None, 50_000) == AttachmentKind.STEP.value
    assert classify_attachment("part.stp", None, 50_000) == AttachmentKind.STEP.value
