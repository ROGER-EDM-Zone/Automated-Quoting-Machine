"""Run the intake path over genuine Outlook messages, when any are present.

Synthetic emails proved the plumbing; they cannot prove the format. Outlook
writes .msg as a Compound File Binary container with dozens of property
streams, and the ways a real one differs from a constructed one — signature
images inline, Exchange addresses instead of SMTP ones, attachments stored by
reference — are exactly the ways parsing quietly goes wrong.

So these tests read whatever real messages are sitting in `real_messages/`
and skip when there are none. The files are never committed: they are real
customers' drawings and addresses, and this repository is public.
"""

from pathlib import Path

import pytest

from app.enums import AttachmentKind
from app.services.email_file import parse_email_file
from app.services.intake import classify_attachment

MESSAGES = sorted(
    p
    for p in (Path(__file__).parent / "real_messages").glob("*")
    if p.suffix.lower() in {".msg", ".eml"}
)

pytestmark = pytest.mark.skipif(
    not MESSAGES,
    reason="no real messages in tests/real_messages/ — drop a .msg or .eml in to run these",
)


@pytest.fixture(params=MESSAGES, ids=lambda p: p.name)
def message(request):
    return parse_email_file(request.param.read_bytes(), request.param.name)


def test_it_parses_and_yields_the_basics(message):
    assert message.subject, "no subject came out"
    assert message.body_text, "no body came out"
    # An Exchange internal address (/O=...) is deliberately dropped rather
    # than recorded as a customer's, so None is a valid answer here.
    if message.sender_email is not None:
        assert "@" in message.sender_email


def test_every_attachment_arrives_with_its_bytes(message):
    for attachment in message.attachments:
        assert attachment.filename
        assert attachment.content_bytes, (
            f"{attachment.filename} came through with a name but no content — "
            "the drawing would reach the estimator empty"
        )


def test_drawings_are_told_apart_from_signature_images(message):
    """Every real email has a logo in the signature. Counting those as
    drawings tells an estimator there are seven when there are three."""
    kinds = {
        a.filename: classify_attachment(a.filename, a.content_type, len(a.content_bytes))
        for a in message.attachments
    }
    for filename, kind in kinds.items():
        if filename.lower().endswith(".pdf"):
            assert kind == AttachmentKind.DRAWING.value, filename
        elif "signature" in filename.lower() or "logo" in filename.lower():
            assert kind != AttachmentKind.DRAWING.value, filename


def test_every_drawing_rasterises_for_the_vision_model(message):
    """The one that matters most. A drawing that parses but will not render
    reaches the estimator looking fine and reads as blank."""
    from app.services.rasterise import rasterise_pdf, to_image_blocks

    drawings = [
        a
        for a in message.attachments
        if classify_attachment(a.filename, a.content_type, len(a.content_bytes))
        == AttachmentKind.DRAWING.value
    ]
    if not drawings:
        pytest.skip("this message carries no drawings")

    for attachment in drawings:
        pages = rasterise_pdf(attachment.content_bytes)
        assert pages, f"{attachment.filename} produced no pages"
        blocks = to_image_blocks(pages)
        assert all(b.base64_data for b in blocks), attachment.filename
