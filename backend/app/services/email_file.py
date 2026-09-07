"""Read an email that somebody dragged out of Outlook.

The mailbox connection is the eventual answer, but it needs an administrator,
a registration and a sign-in before a single real drawing has been read. This
is the way round that: drag the email onto the queue and it becomes an
enquiry.

Both formats Outlook produces are handled.

``.msg``
    Outlook's own format, and what you get dragging a message to the desktop
    or into a browser window. It is a Compound File Binary container — the
    same structure as an old .doc — holding one stream per property. The
    property tags are fixed and documented (MS-OXMSG), so the parts that
    matter can be pulled out without a heavyweight dependency.

``.eml``
    Standard internet mail, which the Python standard library already parses.
    What "Save as" gives, and what most other mail clients produce.

Either way the result is a `GraphMessage` — the identical object the mailbox
poller builds — so everything downstream is the same code on the same data.
An enquiry that arrived by drag-and-drop is indistinguishable from one that
arrived by itself, which is the point: what you learn from one is true of the
other.
"""

from __future__ import annotations

import email
import io
import logging
import re
from datetime import UTC, datetime
from email import policy
from email.utils import parsedate_to_datetime

from app.config import get_settings
from app.services.graph import GraphAttachment, GraphMessage

logger = logging.getLogger(__name__)

#: The first eight bytes of every Compound File Binary container, which is
#: what a .msg is. Checked directly rather than through olefile.isOleFile,
#: because that helper treats a short bytes value as a *filename* and raises
#: FileNotFoundError — turning "this isn't an email" into a crash.
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def looks_like_msg(data: bytes) -> bool:
    return data[:8] == _OLE_MAGIC


class EmailFileError(Exception):
    """The file could not be read as an email. Always says why, in words."""


# --------------------------------------------------------------------------
# Outlook .msg
# --------------------------------------------------------------------------
#: Property tags inside a .msg, from MS-OXMSG. The suffix is the type:
#: 001F is Unicode text, 001E is 8-bit text, 0102 is raw bytes.
_SUBJECT = ("0037",)
_BODY = ("1000",)
_SENDER_EMAIL = ("0C1F", "5D01", "3FFA")
_SENDER_NAME = ("0C1A", "3FFA")
_ATTACH_NAME = ("3707", "3704")
_ATTACH_DATA = ("3701",)
_ATTACH_MIME = ("370E",)
#: Where Outlook keeps the categories it shows as coloured labels.
_CATEGORY_NAMES = ("Keywords",)


def _read_string(ole, path: list[str], tag: str) -> str | None:
    """One text property, trying Unicode then 8-bit."""
    for suffix, encoding in (("001F", "utf-16-le"), ("001E", "latin-1")):
        stream = [*path, f"__substg1.0_{tag}{suffix}"]
        if ole.exists("/".join(stream)):
            try:
                raw = ole.openstream("/".join(stream)).read()
            except OSError:
                continue
            return raw.decode(encoding, errors="replace").rstrip("\x00").strip() or None
    return None


def _first_string(ole, path: list[str], tags: tuple[str, ...]) -> str | None:
    for tag in tags:
        value = _read_string(ole, path, tag)
        if value:
            return value
    return None


def _read_bytes(ole, path: list[str], tag: str) -> bytes | None:
    stream = [*path, f"__substg1.0_{tag}0102"]
    if not ole.exists("/".join(stream)):
        return None
    try:
        return ole.openstream("/".join(stream)).read()
    except OSError:
        return None


def _msg_attachments(ole) -> list[GraphAttachment]:
    attachments: list[GraphAttachment] = []
    for entry in ole.listdir(streams=False, storages=True):
        if not entry or not entry[0].startswith("__attach_version1.0_"):
            continue
        path = [entry[0]]
        filename = _first_string(ole, path, _ATTACH_NAME)
        data = _read_bytes(ole, path, _ATTACH_DATA[0])
        if not filename:
            continue
        attachments.append(
            GraphAttachment(
                filename=filename,
                content_type=_first_string(ole, path, _ATTACH_MIME),
                # An attachment Outlook stored by reference rather than by
                # value has no bytes. Keep it — the name still tells an
                # estimator a drawing was meant to be here.
                content_bytes=data or b"",
            )
        )
    return attachments


def parse_msg(data: bytes, *, filename: str = "message.msg") -> GraphMessage:
    try:
        import olefile
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise EmailFileError(
            "Reading Outlook .msg files needs the 'olefile' package. "
            "Run the setup again, or save the email as .eml instead."
        ) from exc

    if not looks_like_msg(data):
        raise EmailFileError(
            f"{filename} is not an Outlook message file. Drag the email "
            "straight out of Outlook, or use File → Save As."
        )

    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - a corrupt container is user input
        raise EmailFileError(
            f"{filename} looks like an Outlook message but could not be "
            f"opened: {exc}. Try dragging it out of Outlook again."
        ) from exc
    try:
        subject = _first_string(ole, [], _SUBJECT)
        body = _first_string(ole, [], _BODY)
        sender = _first_string(ole, [], _SENDER_EMAIL)
        sender_name = _first_string(ole, [], _SENDER_NAME)
        attachments = _msg_attachments(ole)

        received = None
        try:
            stamps = ole.getmtime("__properties_version1.0")
            received = stamps if isinstance(stamps, datetime) else None
        except Exception:  # noqa: BLE001 - a missing timestamp is not a failure
            received = None
    finally:
        ole.close()

    # Outlook writes Exchange internal addresses for colleagues, which are not
    # email addresses and must not be recorded as a customer's.
    if sender and sender.startswith("/O="):
        logger.info("Sender in %s is an Exchange address; leaving it unmatched", filename)
        sender = None

    return GraphMessage(
        message_id=f"file:{filename}",
        subject=subject,
        body_text=body,
        sender_email=sender,
        sender_name=sender_name,
        received_at=received or datetime.now(UTC),
        categories=[get_settings().graph_rfq_category],
        attachments=attachments,
    )


# --------------------------------------------------------------------------
# Standard .eml
# --------------------------------------------------------------------------
_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def parse_eml(data: bytes, *, filename: str = "message.eml") -> GraphMessage:
    try:
        message = email.message_from_bytes(data, policy=policy.default)
    except Exception as exc:  # noqa: BLE001 - malformed mail is a user error
        raise EmailFileError(f"{filename} could not be read as an email: {exc}") from exc

    if not message.get("subject") and not message.get("from"):
        raise EmailFileError(
            f"{filename} has no subject and no sender, so it is probably not an email file."
        )

    from_header = str(message.get("from") or "")
    found = _ADDRESS.search(from_header)
    sender = found.group(0) if found else None
    sender_name = from_header.split("<")[0].strip().strip('"') or None

    body = None
    try:
        part = message.get_body(preferencelist=("plain", "html"))
        if part is not None:
            body = part.get_content()
    except Exception:  # noqa: BLE001 - fall through to the raw payload
        body = None
    if body is None and not message.is_multipart():
        body = message.get_payload(decode=True) or b""
        body = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body

    attachments = []
    for part in message.iter_attachments():
        name = part.get_filename()
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        attachments.append(
            GraphAttachment(
                filename=name,
                content_type=part.get_content_type(),
                content_bytes=payload,
            )
        )

    received = None
    if message.get("date"):
        try:
            received = parsedate_to_datetime(message["date"])
        except (TypeError, ValueError):
            received = None

    return GraphMessage(
        message_id=f"file:{filename}",
        subject=message.get("subject"),
        body_text=body,
        sender_email=sender,
        sender_name=sender_name,
        received_at=received or datetime.now(UTC),
        categories=[get_settings().graph_rfq_category],
        attachments=attachments,
    )


def parse_email_file(data: bytes, filename: str) -> GraphMessage:
    """Read a dragged email, working out which kind it is.

    Chooses on content rather than on the file extension: a browser upload
    frequently loses or mangles the name, and an email saved with the wrong
    suffix should still work.
    """
    if not data:
        raise EmailFileError(f"{filename} is empty.")

    if looks_like_msg(data):
        return parse_msg(data, filename=filename)
    return parse_eml(data, filename=filename)
