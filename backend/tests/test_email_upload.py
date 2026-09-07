"""Dragging an email in, instead of waiting for a mailbox connection.

The point of these is the last one: an enquiry that arrived by hand must be
identical to one that arrived by itself. If the two paths diverge, testing the
manual one teaches nothing about the automatic one, and the whole reason for
doing it this way disappears.
"""

from email.message import EmailMessage
from functools import lru_cache

import pytest

from app.models import Enquiry
from app.services.email_file import EmailFileError, parse_email_file, parse_eml


@lru_cache(maxsize=4)
def drawing_pdf(number: str = "67980") -> bytes:
    """A real PDF. Cached because PyMuPDF stamps a creation time into every
    file it writes, so calling this twice would produce two different
    documents — and the point of some of these tests is that the same email
    really is the same bytes."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((60, 60), f"DRAWING No. {number}", fontsize=16)
    page.insert_text((60, 90), "MATERIAL: EN16")
    data = doc.tobytes()
    doc.close()
    return data


def rfq_eml(
    *,
    subject="FW: RFQ",
    sender="Worrall, Steve <Steve.Worrall@ricardo.com>",
    body="Please quote 15 off drawing 67980.",
    attachments=(("67980_iss1 - OIL FEED PLATE.pdf", None),),
) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = "sales@edmzone.co.uk"
    message["Date"] = "Thu, 03 Sep 2026 09:08:32 +0000"
    message.set_content(body)
    for name, data in attachments:
        message.add_attachment(
            data if data is not None else drawing_pdf(),
            maintype="application",
            subtype="pdf",
            filename=name,
        )
    return message.as_bytes()


# --------------------------------------------------------------------------
# Reading the file
# --------------------------------------------------------------------------
def test_an_eml_gives_up_everything_intake_needs():
    message = parse_email_file(rfq_eml(), "rfq.eml")

    assert message.subject == "FW: RFQ"
    assert message.sender_email == "Steve.Worrall@ricardo.com"
    assert message.sender_name == "Worrall, Steve"
    assert "15 off" in message.body_text
    assert message.received_at.year == 2026


def test_the_drawing_arrives_with_its_actual_bytes():
    """The whole reason this route exists. Reading a mailbox through a chat
    connector gives the filename but not the file; a dragged email gives both."""
    message = parse_email_file(rfq_eml(), "rfq.eml")

    assert len(message.attachments) == 1
    attachment = message.attachments[0]
    assert attachment.filename == "67980_iss1 - OIL FEED PLATE.pdf"
    assert attachment.content_bytes.startswith(b"%PDF")
    assert len(attachment.content_bytes) > 500


def test_several_drawings_all_come_through():
    data = rfq_eml(
        attachments=(("3888-PRT-001.pdf", None), ("3888-PRT-002.pdf", None)),
    )
    message = parse_email_file(data, "rfq.eml")
    assert {a.filename for a in message.attachments} == {
        "3888-PRT-001.pdf",
        "3888-PRT-002.pdf",
    }
    assert all(a.content_bytes.startswith(b"%PDF") for a in message.attachments)


def test_it_carries_the_rfq_category_so_intake_accepts_it():
    # A file somebody dragged in was chosen deliberately; requiring them to
    # have tagged it first would be asking twice.
    assert parse_email_file(rfq_eml(), "rfq.eml").categories == ["RFQ"]


def test_the_format_is_decided_by_content_not_by_the_file_name():
    # Browsers mangle filenames, and people rename things.
    message = parse_email_file(rfq_eml(), "no-extension-at-all")
    assert message.subject == "FW: RFQ"


def test_an_empty_file_says_so_plainly():
    with pytest.raises(EmailFileError, match="empty"):
        parse_email_file(b"", "nothing.eml")


def test_something_that_is_not_an_email_says_so_plainly():
    with pytest.raises(EmailFileError, match="probably not an email"):
        parse_email_file(b"just some text in a file", "notes.txt")


def test_a_file_claiming_to_be_a_msg_but_is_not_gets_a_useful_message():
    from app.services.email_file import parse_msg

    with pytest.raises(EmailFileError, match="not an Outlook message"):
        parse_msg(b"not really a msg", filename="thing.msg")


def test_a_corrupt_msg_gives_words_rather_than_a_crash():
    """olefile treats a short bytes value as a filename and raises
    FileNotFoundError. That must never reach the user."""
    from app.services.email_file import _OLE_MAGIC

    with pytest.raises(EmailFileError, match="could not be opened"):
        parse_email_file(_OLE_MAGIC + b"truncated nonsense", "broken.msg")


def test_an_html_only_email_still_yields_a_body():
    message = EmailMessage()
    message["Subject"] = "RFQ"
    message["From"] = "buyer@customer.example"
    message.set_content("<p>Quote please, 4 off</p>", subtype="html")
    parsed = parse_eml(message.as_bytes(), filename="html.eml")
    assert "4 off" in parsed.body_text


def test_an_email_with_no_attachments_is_still_read():
    message = parse_email_file(rfq_eml(attachments=()), "bare.eml")
    assert message.subject == "FW: RFQ"
    assert message.attachments == []


# --------------------------------------------------------------------------
# Through the endpoint
# --------------------------------------------------------------------------
def upload(client, name, data):
    return client.post(
        "/api/intake/upload",
        files=[("files", (name, data, "application/octet-stream"))],
    )


def test_dragging_an_email_in_creates_an_enquiry(api):
    client, db, *_ = api

    body = upload(client, "rfq.eml", rfq_eml()).json()

    assert body["new_count"] == 1
    assert body["failed"] == []
    entry = body["ingested"][0]
    assert entry["drawings"] == 1
    assert entry["subject"] == "FW: RFQ"

    enquiry = db.get(Enquiry, entry["enquiry_id"])
    assert enquiry.sender_email == "Steve.Worrall@ricardo.com"
    assert [a.filename for a in enquiry.attachments] == ["67980_iss1 - OIL FEED PLATE.pdf"]


def test_the_same_email_dragged_twice_does_not_duplicate(api):
    client, _db, *_ = api

    first = upload(client, "rfq.eml", rfq_eml()).json()
    second = upload(client, "rfq.eml", rfq_eml()).json()

    assert first["new_count"] == 1
    assert second["new_count"] == 0
    assert second["already_known"] == 1
    assert second["ingested"][0]["enquiry_id"] == first["ingested"][0]["enquiry_id"]


def test_one_unreadable_file_does_not_lose_the_rest_of_the_batch(api):
    client, *_ = api

    response = client.post(
        "/api/intake/upload",
        files=[
            ("files", ("good.eml", rfq_eml(), "application/octet-stream")),
            ("files", ("rubbish.txt", b"not an email", "application/octet-stream")),
            (
                "files",
                ("also-good.eml", rfq_eml(subject="RFQ 2"), "application/octet-stream"),
            ),
        ],
    )
    body = response.json()

    assert body["new_count"] == 2
    assert len(body["failed"]) == 1
    assert body["failed"][0]["filename"] == "rubbish.txt"
    assert body["failed"][0]["reason"]


def test_a_dragged_enquiry_is_identical_to_a_polled_one(api):
    """The one that matters. Both doors, same room.

    If these ever diverge, what an estimator learns from a dragged-in RFQ
    stops being true of the ones that arrive on their own.
    """
    client, db, *_ = api
    from app.services.intake import ingest_message

    dragged = upload(client, "rfq.eml", rfq_eml()).json()["ingested"][0]

    # The identical message, arriving the way the mailbox delivers it.
    polled = ingest_message(db, parse_email_file(rfq_eml(subject="Polled"), "x.eml"))
    db.commit()

    a = db.get(Enquiry, dragged["enquiry_id"])
    b = polled.enquiry

    assert a.sender_email == b.sender_email
    assert a.status == b.status
    assert [x.kind for x in a.attachments] == [x.kind for x in b.attachments]
    assert [x.content_hash for x in a.attachments] == [x.content_hash for x in b.attachments]


# --------------------------------------------------------------------------
# The three ways in
# --------------------------------------------------------------------------
def upload_to(client, lane, name="rfq.eml", data=None):
    return client.post(
        "/api/intake/upload",
        files=[("files", (name, data or rfq_eml(), "application/octet-stream"))],
        data={"lane": lane},
    )


def test_the_zone_it_was_dropped_on_is_recorded(api):
    client, db, *_ = api

    body = upload_to(client, "wire_edm").json()

    assert body["ingested"][0]["lane"] == "wire_edm"
    assert db.get(Enquiry, body["ingested"][0]["enquiry_id"]).intake_lane == "wire_edm"


def test_dropping_without_choosing_a_zone_still_works(api):
    client, db, *_ = api

    body = upload(client, "rfq.eml", rfq_eml()).json()

    assert body["new_count"] == 1
    assert db.get(Enquiry, body["ingested"][0]["enquiry_id"]).intake_lane is None


def test_an_unknown_zone_is_rejected_rather_than_recorded(api):
    client, *_ = api

    response = upload_to(client, "wire_edn")  # a typo, not a lane

    assert response.status_code == 422
    assert "wire_edm" in response.json()["detail"]


@pytest.mark.parametrize(
    "lane,job_type,processes,constrained",
    [
        ("wire_edm", "service_only", ["wire_edm"], True),
        ("spark_erode", "service_only", ["spark_erode"], True),
        # Full supply says who buys the material, not what the machines do,
        # so the routing is still the classifier's to work out.
        ("full_supply", "full_supply", None, False),
    ],
)
def test_the_zone_decides_the_job_type_and_routing(db, lane, job_type, processes, constrained):
    from app.models import Enquiry, Part, utcnow
    from app.services.extraction import _apply_intake_lane

    enquiry = Enquiry(
        outlook_message_id=f"lane-{lane}",
        subject="RFQ",
        received_at=utcnow(),
        intake_lane=lane,
    )
    db.add(enquiry)
    db.flush()
    part = Part(enquiry_id=enquiry.id)
    db.add(part)
    db.flush()

    _apply_intake_lane(db, part)

    assert part.job_type == job_type
    assert part.process_mix == processes
    assert part.process_mix_constrained is constrained


def test_a_part_with_no_zone_is_left_for_the_classifier(db):
    from app.models import Enquiry, Part, utcnow
    from app.services.extraction import _apply_intake_lane

    enquiry = Enquiry(outlook_message_id="no-lane", received_at=utcnow())
    db.add(enquiry)
    db.flush()
    part = Part(enquiry_id=enquiry.id, job_type="ambiguous")
    db.add(part)
    db.flush()

    _apply_intake_lane(db, part)

    assert part.job_type == "ambiguous"
    assert part.process_mix is None


def test_re_dropping_an_email_does_not_relabel_a_corrected_job(api):
    """An estimator who fixed the job type in the workspace must not be
    silently overruled by somebody dragging the same email in again."""
    client, db, *_ = api

    first = upload_to(client, "wire_edm").json()["ingested"][0]
    enquiry_id = first["enquiry_id"]

    db.get(Enquiry, enquiry_id).intake_lane = "full_supply"
    db.commit()
    upload_to(client, "spark_erode")

    assert db.get(Enquiry, enquiry_id).intake_lane == "full_supply"


def test_an_unrecognised_zone_on_an_old_enquiry_is_ignored_not_obeyed(db):
    """A lane name that no longer exists must leave the part alone rather
    than crash extraction or apply something arbitrary."""
    from app.models import Enquiry, Part, utcnow
    from app.services.extraction import _apply_intake_lane

    enquiry = Enquiry(outlook_message_id="old-lane", received_at=utcnow(), intake_lane="laser_cut")
    db.add(enquiry)
    db.flush()
    part = Part(enquiry_id=enquiry.id, job_type="ambiguous")
    db.add(part)
    db.flush()

    _apply_intake_lane(db, part)

    assert part.job_type == "ambiguous"
