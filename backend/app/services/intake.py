"""Intake (spec stage 1).

Tagged mail arrives, the email and its attachments are persisted, each
attachment is classified by kind, and duplicate / version-conflict detection
runs before anything is extracted.

Deliberately does not call the AI. Intake's job is to prove the plumbing and
land the record safely; getting that wrong loses an enquiry, which is worse
than mis-reading one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import AttachmentKind, EnquiryStatus
from app.models import Attachment, Customer, Enquiry, utcnow
from app.services.classification import duplicate_check
from app.services.graph import GraphMessage
from app.services.history import parse_customer_reference
from app.services.storage import Storage, attachment_key, content_hash, get_storage

logger = logging.getLogger(__name__)

_DRAWING_EXTENSIONS = {".pdf", ".dwg", ".dxf", ".tif", ".tiff", ".png", ".jpg", ".jpeg"}
_STEP_EXTENSIONS = {".step", ".stp", ".iges", ".igs", ".x_t", ".sldprt", ".ipt", ".3dm"}
_SPREADSHEET_EXTENSIONS = {".xls", ".xlsx", ".xlsm", ".csv"}

#: Inline signature images and logos that are not drawings.
_NOISE = re.compile(r"(image\d{3,}|logo|signature|footer|banner)", re.I)

#: Consumer and ISP domains. Real customers do send from these — one of EDM
#: Zone's does — but the domain identifies the provider, not the company, so
#: matching a customer on it would map every other BT subscriber to them too.
#: Mail from these is left unmatched for a human to attach to a customer.
#: SHA-256 of nothing. An attachment the mail server could not give us has
#: this hash, and so does every other one — it therefore identifies nothing
#: and must never be used to match files against each other.
EMPTY_CONTENT_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

GENERIC_EMAIL_DOMAINS = frozenset(
    {
        "aol.com",
        "btconnect.com",
        "btinternet.com",
        "gmail.com",
        "googlemail.com",
        "hotmail.co.uk",
        "hotmail.com",
        "icloud.com",
        "live.co.uk",
        "live.com",
        "mail.com",
        "me.com",
        "outlook.com",
        "protonmail.com",
        "sky.com",
        "talktalk.net",
        "virginmedia.com",
        "yahoo.co.uk",
        "yahoo.com",
    }
)

#: "From:" lines inside a forwarded body, e.g.
#:     From: Kirsty Bruce <KBruce@act-group.co.uk>
#:     From: bloochip@btinternet.com <bloochip@btinternet.com>
_FORWARD_FROM = re.compile(
    r"^\s*From:\s*(?P<name>[^<\n]*?)\s*(?:<(?P<bracketed>[^>\n]+)>|(?P<bare>[\w.+-]+@[\w.-]+))",
    re.I | re.M,
)


@dataclass
class ForwardedOrigin:
    """Who actually sent an RFQ that reached us via an internal forward."""

    email: str
    name: str | None = None


@dataclass
class PollResult:
    """What one sweep of the mailbox found."""

    checked: int = 0
    ingested: list[int] = field(default_factory=list)
    already_known: int = 0
    failed: list[str] = field(default_factory=list)

    @property
    def new_count(self) -> int:
        return len(self.ingested)


@dataclass
class IntakeResult:
    enquiry: Enquiry
    created: bool
    attachments_stored: int
    drawings_found: int


def classify_attachment(filename: str, content_type: str | None, size_bytes: int) -> str:
    """Decide what kind of attachment this is.

    STEP/3D files are recognised and stored but not read — the spec defers CAD
    reading, and recognising the kind now is what keeps that a later feature
    rather than a rebuild.
    """
    name = filename.lower()
    suffix = name[name.rfind(".") :] if "." in name else ""

    if suffix in _STEP_EXTENSIONS:
        return AttachmentKind.STEP.value
    if suffix in _SPREADSHEET_EXTENSIONS:
        return AttachmentKind.SPREADSHEET.value
    if suffix in _DRAWING_EXTENSIONS:
        # A 4KB inline PNG called image003.png is a signature, not a drawing.
        if suffix in {".png", ".jpg", ".jpeg", ".gif"} and (
            _NOISE.search(name) or size_bytes < 20_000
        ):
            return AttachmentKind.OTHER.value
        return AttachmentKind.DRAWING.value
    return AttachmentKind.OTHER.value


def parse_forwarded_origin(
    body_text: str | None, *, internal_domains: set[str]
) -> ForwardedOrigin | None:
    """Find the original sender inside a forwarded email chain.

    At EDM Zone a large share of RFQs reach the sales mailbox as a forward from
    a colleague — "RFQ to process Wire EDM" over the top of the customer's
    email. Matching the customer on the envelope sender would file those under
    our own company, so the chain is read for the first sender who is not one
    of us.
    """
    if not body_text:
        return None

    for match in _FORWARD_FROM.finditer(body_text):
        address = (match.group("bracketed") or match.group("bare") or "").strip()
        if "@" not in address:
            continue
        domain = address.rsplit("@", 1)[1].lower().strip(">.,;")
        if domain in internal_domains:
            # A forward of a forward: keep looking for the outside party.
            continue
        name = (match.group("name") or "").strip(" \"'\t") or None
        # A name that is just the address again tells us nothing.
        if name and name.lower() == address.lower():
            name = None
        return ForwardedOrigin(email=address, name=name)
    return None


def _forwarder_note(body_text: str | None) -> str | None:
    """The colleague's own words above a forwarded chain.

    "RFQ to process Wire EDM" is a routing instruction from someone who knows
    the shop, and is worth more to the classifier than most of the email under
    it — but it is ours, not the customer's, and is stored separately so the
    two are never confused.
    """
    if not body_text:
        return None
    match = _FORWARD_FROM.search(body_text)
    head = body_text[: match.start()] if match else body_text
    # Drop the separator rule some clients put above the chain.
    head = re.sub(r"[_\-]{5,}", " ", head)
    cleaned = " ".join(head.split())
    return cleaned[:500] or None


def resolve_customer(db: Session, sender_email: str | None) -> Customer | None:
    """Match a sender to a customer by email domain.

    No fuzzy matching on company names: quoting one customer's enquiry against
    another's standing margin is a mistake worth avoiding by being strict.
    Generic ISP domains never match, because they identify the mail provider
    rather than the company.
    """
    if not sender_email or "@" not in sender_email:
        return None
    domain = sender_email.rsplit("@", 1)[1].lower()
    if domain in GENERIC_EMAIL_DOMAINS:
        return None
    return db.scalars(select(Customer).where(Customer.domain == domain)).first()


def ingest_message(
    db: Session,
    message: GraphMessage,
    *,
    storage: Storage | None = None,
    require_category: str | None = None,
) -> IntakeResult:
    """Persist one tagged email as an enquiry.

    Idempotent on `outlook_message_id`: Graph re-delivers notifications, and a
    duplicate delivery must not create a second enquiry.
    """
    storage = storage or get_storage()

    if require_category and require_category not in message.categories:
        raise ValueError(f"Message {message.message_id} is not tagged '{require_category}'")

    existing = db.scalars(
        select(Enquiry).where(Enquiry.outlook_message_id == message.message_id)
    ).first()
    if existing is not None:
        logger.info("Message %s already ingested as enquiry %s", message.message_id, existing.id)
        return IntakeResult(
            enquiry=existing,
            created=False,
            attachments_stored=len(existing.attachments),
            drawings_found=sum(
                1 for a in existing.attachments if a.kind == AttachmentKind.DRAWING.value
            ),
        )

    # An RFQ forwarded by a colleague carries our address on the envelope. The
    # customer is inside the chain, and quoting the enquiry against our own
    # company would be worse than not matching at all.
    settings = get_settings()
    internal_domains = settings.own_domains()
    sender_email = message.sender_email
    forwarded_by: str | None = None
    internal_note: str | None = None

    sender_domain = (
        sender_email.rsplit("@", 1)[1].lower() if sender_email and "@" in sender_email else None
    )
    if sender_domain and sender_domain in internal_domains:
        origin = parse_forwarded_origin(message.body_text, internal_domains=internal_domains)
        if origin is not None:
            forwarded_by = sender_email
            sender_email = origin.email
            internal_note = _forwarder_note(message.body_text)
            logger.info(
                "Message %s was forwarded by %s; treating %s as the customer",
                message.message_id,
                forwarded_by,
                sender_email,
            )

    customer = resolve_customer(db, sender_email)
    enquiry = Enquiry(
        customer_id=customer.id if customer else None,
        outlook_message_id=message.message_id,
        subject=message.subject,
        body_text=message.body_text,
        sender_email=sender_email,
        forwarded_by=forwarded_by,
        internal_note=internal_note,
        received_at=message.received_at or utcnow(),
        tagged_at=utcnow(),
        status=EnquiryStatus.RECEIVED.value,
        customer_reference=parse_customer_reference(message.body_text),
    )
    db.add(enquiry)
    db.flush()

    stored = 0
    drawings = 0
    seen_hashes: set[str] = set()
    for item in message.attachments:
        digest = content_hash(item.content_bytes)
        # Two different files that both came through empty hash identically —
        # every empty byte string does. Deduping on that would drop the second
        # drawing off an enquiry silently, which is the one failure mode worth
        # more than a duplicate row. Empty attachments are always kept.
        if item.content_bytes and digest in seen_hashes:
            # The same file attached twice in one mail. One row is enough.
            continue
        if item.content_bytes:
            seen_hashes.add(digest)

        kind = classify_attachment(item.filename, item.content_type, len(item.content_bytes))
        key = attachment_key(enquiry.id, item.filename, digest)
        blob_uri = storage.put(key, item.content_bytes, item.content_type)

        db.add(
            Attachment(
                enquiry_id=enquiry.id,
                filename=item.filename,
                blob_uri=blob_uri,
                mime_type=item.content_type,
                kind=kind,
                content_hash=digest,
                size_bytes=len(item.content_bytes),
            )
        )
        stored += 1
        if kind == AttachmentKind.DRAWING.value:
            drawings += 1

    db.flush()
    duplicate_check(db, enquiry)

    if drawings == 0:
        from app.enums import FlagCategory, FlagSeverity
        from app.services.flags import raise_flag

        enquiry.status = EnquiryStatus.NEEDS_ATTENTION.value
        raise_flag(
            db,
            enquiry_id=enquiry.id,
            category=FlagCategory.EXTRACTION_UNCERTAINTY.value,
            severity=FlagSeverity.BLOCK.value,
            message=(
                "No drawing attachments on this enquiry. It may be a covering "
                "email, a query, or the drawings may be on a link."
            ),
            dedupe_key="no_drawings_at_intake",
        )

    db.flush()
    logger.info(
        "Ingested message %s as enquiry %s (%d attachments, %d drawings)",
        message.message_id,
        enquiry.id,
        stored,
        drawings,
    )
    return IntakeResult(
        enquiry=enquiry, created=True, attachments_stored=stored, drawings_found=drawings
    )


def poll_mailbox(
    db: Session,
    *,
    client=None,
    storage: Storage | None = None,
    limit: int = 25,
) -> PollResult:
    """Pull tagged mail the app has not already seen.

    Graph pushes a notification when tagged mail arrives, but a push can be
    missed — the subscription lapses, the app is restarted, the notification
    never lands. Polling is the backstop, and it is also what lets the system
    run before there is a public address to push to at all.

    Safe to call as often as you like: ingest is idempotent on the Outlook
    message id, so a message already in the system is counted and skipped
    rather than duplicated.
    """
    from app.services.graph import GraphError, get_graph_client

    client = client or get_graph_client()
    settings = get_settings()
    result = PollResult()

    messages = client.list_tagged_messages(category=settings.graph_rfq_category, limit=limit)
    result.checked = len(messages)

    known = {
        row[0]
        for row in db.execute(
            select(Enquiry.outlook_message_id).where(
                Enquiry.outlook_message_id.in_([m["id"] for m in messages] or [""])
            )
        ).all()
    }

    for summary in messages:
        message_id = summary["id"]
        if message_id in known:
            result.already_known += 1
            continue
        try:
            message = client.get_message(message_id)
            ingested = ingest_message(
                db, message, storage=storage, require_category=settings.graph_rfq_category
            )
            db.flush()
            result.ingested.append(ingested.enquiry.id)
        except (GraphError, ValueError) as exc:
            # One bad message must not stop the rest of the sweep.
            logger.exception("Could not ingest message %s", message_id)
            result.failed.append(f"{summary.get('subject') or message_id}: {exc}")

    logger.info(
        "Mailbox poll: %d checked, %d new, %d already known, %d failed",
        result.checked,
        result.new_count,
        result.already_known,
        len(result.failed),
    )
    return result


def duplicate_attachment_matches(db: Session, enquiry: Enquiry) -> list[Attachment]:
    """Attachments byte-identical to ones on other enquiries.

    A stronger duplicate signal than drawing number plus revision, because it
    catches the case where the title block was never read.
    """
    # An attachment that arrived empty hashes the same as every other empty
    # one, so matching on it would report unrelated enquiries as the same RFQ.
    hashes = [
        a.content_hash
        for a in enquiry.attachments
        if a.content_hash and a.content_hash != EMPTY_CONTENT_HASH
    ]
    if not hashes:
        return []
    return list(
        db.scalars(
            select(Attachment).where(
                Attachment.content_hash.in_(hashes),
                Attachment.enquiry_id != enquiry.id,
            )
        ).all()
    )
