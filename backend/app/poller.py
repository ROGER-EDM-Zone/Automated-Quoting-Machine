"""Check the mailbox on a timer, so RFQs arrive without anyone asking.

Intake can already be driven three ways: a Graph webhook, the "Check for new
enquiries" button, and the command line. All three need something to happen
first — a notification to arrive, or a person to be at the screen. This is the
fourth, and it is the one that makes the system feel like it is running rather
than being operated.

Deliberately unclever. No scheduler library, no job queue, no persistence: one
loop that sleeps, wakes, polls, and logs. On a shop-floor PC quoting a few
dozen enquiries a week, anything more is machinery to maintain for no gain.

Three rules it does follow, because each one is a way this could quietly go
wrong:

  * **It never runs when Graph is not configured.** Otherwise the log fills
    with failures nobody can act on and everyone learns to ignore it.
  * **One poll at a time.** A slow mailbox must not cause polls to pile up on
    top of each other and ingest the same message twice.
  * **A failed poll is logged and the loop continues.** A network blip at 3am
    must not silently stop the app checking mail for the rest of the week.
"""

from __future__ import annotations

import asyncio
import logging

from app.config import Settings
from app.db import SessionLocal
from app.services.graph import GraphError, GraphNeedsSignIn, get_graph_client
from app.services.intake import poll_mailbox

logger = logging.getLogger(__name__)


def poll_once() -> str:
    """One pass over the mailbox. Returns a line worth logging."""
    client = get_graph_client()
    db = SessionLocal()
    try:
        result = poll_mailbox(db, client=client)
        db.commit()
    finally:
        db.close()

    if result.failed:
        for failure in result.failed:
            logger.warning("intake could not read a message: %s", failure)

    if result.new_count:
        return (
            f"pulled {result.new_count} new "
            f"enquir{'y' if result.new_count == 1 else 'ies'} "
            f"({result.already_known} already known)"
        )
    return f"nothing new ({result.already_known} already known)"


async def run(settings: Settings) -> None:
    """Poll forever. Cancelled on shutdown."""
    interval = max(60, settings.mailbox_poll_seconds)
    logger.info("Mailbox poll started — every %d seconds", interval)

    while True:
        try:
            # Blocking HTTP and database work, so it goes to a worker thread
            # rather than stalling the web server for everyone using it.
            summary = await asyncio.to_thread(poll_once)
            logger.info("Mailbox poll: %s", summary)
        except asyncio.CancelledError:
            logger.info("Mailbox poll stopped")
            raise
        except GraphNeedsSignIn as exc:
            # Recoverable by a human, and shouting every minute would not help
            # them notice any sooner.
            logger.warning("Mailbox poll needs a sign-in: %s", exc)
        except GraphError as exc:
            logger.warning("Mailbox poll failed: %s", exc)
        except Exception:
            # Never let one bad message stop the app checking mail for good.
            logger.exception("Mailbox poll hit an unexpected error; continuing")

        await asyncio.sleep(interval)


def should_run(settings: Settings) -> bool:
    """Whether polling is both wanted and possible."""
    if not settings.mailbox_poll_enabled:
        return False
    if not (settings.graph_tenant_id and settings.graph_client_id):
        return False
    return bool(settings.graph_quoting_mailbox)
