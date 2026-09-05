"""Ingest RFQs from a JSON file, when the app cannot yet read the mailbox itself.

    python -m scripts.import_messages inbox.json

The permanent path is `scripts/check_graph.py --poll`, which needs an Entra ID
app registration so the app has its own identity. Getting that registration is
somebody else's queue, and waiting for it means waiting to find out whether any
of this reads a real drawing correctly.

So this is the same pipeline with a different front door. It takes messages
already exported from the mailbox and hands them to `ingest_message` — the
identical function the live poller calls, with the identical forwarded-RFQ
handling, attachment classification and duplicate detection. Nothing here is a
special case, which is the point: what you learn from this import is true of
the real thing.

The file is a JSON list of messages:

    [
      {
        "message_id": "AAMk...",              required, and what makes this
                                              idempotent — re-importing the
                                              same file changes nothing
        "subject": "RFQ 27-0004",
        "body_text": "Can you quote ...",
        "sender_email": "buyer@customer.com",
        "sender_name": "A Buyer",
        "received_at": "2026-09-03T11:12:03Z",
        "categories": ["RFQ"],
        "attachments": [
          {
            "filename": "67980.pdf",
            "content_type": "application/pdf",
            "content_base64": "JVBERi0..."   omit to record the attachment
          }                                   as present but unread
        ]
      }
    ]
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import datetime
from pathlib import Path

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.services.graph import GraphAttachment, GraphMessage
from app.services.intake import ingest_message

TICK = "  \033[32m✓\033[0m"
CROSS = "  \033[31m✗\033[0m"
INFO = "  \033[2m·\033[0m"


def parse_received(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def to_graph_message(raw: dict) -> GraphMessage:
    """Build the same object the Graph client would have built."""
    attachments = []
    for item in raw.get("attachments") or []:
        encoded = item.get("content_base64")
        content = base64.b64decode(encoded) if encoded else b""
        attachments.append(
            GraphAttachment(
                filename=item["filename"],
                content_type=item.get("content_type"),
                content_bytes=content,
            )
        )

    return GraphMessage(
        message_id=raw["message_id"],
        subject=raw.get("subject"),
        body_text=raw.get("body_text"),
        sender_email=raw.get("sender_email"),
        sender_name=raw.get("sender_name"),
        received_at=parse_received(raw.get("received_at")),
        # Default to the configured tag: a message exported by hand was
        # chosen deliberately, so requiring the tag again would only reject
        # work somebody already decided was an RFQ.
        categories=raw.get("categories") or [get_settings().graph_rfq_category],
        attachments=attachments,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="JSON file of messages")
    parser.add_argument(
        "--require-tag",
        action="store_true",
        help="skip messages not carrying the configured RFQ category",
    )
    args = parser.parse_args()

    if not args.file.exists():
        print(f"No such file: {args.file}", file=sys.stderr)
        return 1

    try:
        payload = json.loads(args.file.read_text())
    except json.JSONDecodeError as exc:
        print(f"{args.file} is not valid JSON: {exc}", file=sys.stderr)
        return 1

    if not isinstance(payload, list):
        print("Expected a JSON list of messages.", file=sys.stderr)
        return 1

    settings = get_settings()
    init_db()
    db = SessionLocal()
    created = existing = failed = 0

    try:
        for raw in payload:
            subject = raw.get("subject") or "(no subject)"
            try:
                result = ingest_message(
                    db,
                    to_graph_message(raw),
                    require_category=settings.graph_rfq_category if args.require_tag else None,
                )
                db.commit()
            except (KeyError, ValueError) as exc:
                db.rollback()
                failed += 1
                print(f"{CROSS} {subject}: {exc}")
                continue

            if result.created:
                created += 1
                print(f"{TICK} {subject}")
                print(
                    f"      enquiry {result.enquiry.id} · "
                    f"{result.attachments_stored} attachment(s), "
                    f"{result.drawings_found} drawing(s)"
                )
                if result.enquiry.forwarded_by:
                    print(f"      {INFO} forwarded by {result.enquiry.forwarded_by}")
                if result.enquiry.customer is None:
                    print(f"      {INFO} no customer matched — needs assigning")
            else:
                existing += 1
                print(f"{INFO} {subject} — already ingested as {result.enquiry.id}")
    finally:
        db.close()

    print(f"\n{created} new, {existing} already known, {failed} failed.\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
