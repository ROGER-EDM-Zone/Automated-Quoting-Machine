"""Check the Outlook connection, and say precisely what is wrong if it isn't.

Run this the moment you have credentials, before anything else:

    python -m scripts.check_graph

It walks the connection one step at a time — settings, token, mailbox, category,
tagged mail — and stops at the first thing that fails with a specific fix rather
than a stack trace. Connecting a mailbox has about six ways to go subtly wrong,
and five of them otherwise present as "no enquiries appeared".

Add --poll to also pull any waiting mail into the database once the checks pass.

Nothing here writes to Outlook. It reads, and it cannot send.
"""

from __future__ import annotations

import argparse

from app.config import get_settings
from app.services.graph import GraphError, GraphNotConfigured, get_graph_client

TICK = "  \033[32m✓\033[0m"
CROSS = "  \033[31m✗\033[0m"
INFO = "  \033[2m·\033[0m"


def ok(message: str) -> None:
    print(f"{TICK} {message}")


def fail(message: str, fix: str) -> None:
    print(f"{CROSS} {message}")
    for line in fix.strip().splitlines():
        print(f"      {line.strip()}")


def note(message: str) -> None:
    print(f"{INFO} \033[2m{message}\033[0m")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--poll",
        action="store_true",
        help="after the checks pass, pull waiting mail into the database",
    )
    args = parser.parse_args()

    settings = get_settings()
    print("\nChecking the Outlook connection\n")

    # --- 1. settings ------------------------------------------------------
    missing = [
        name
        for name, value in (
            ("AQM_GRAPH_TENANT_ID", settings.graph_tenant_id),
            ("AQM_GRAPH_CLIENT_ID", settings.graph_client_id),
            ("AQM_GRAPH_CLIENT_SECRET", settings.graph_client_secret),
            ("AQM_GRAPH_QUOTING_MAILBOX", settings.graph_quoting_mailbox),
        )
        if not value
    ]
    if missing:
        fail(
            f"{len(missing)} setting(s) missing: {', '.join(missing)}",
            """
            Put them in backend/.env — see .env.example for the shape.
            Your M365 admin gets the first three from the Entra ID app
            registration; the fourth is the shared mailbox address.
            """,
        )
        return 1
    ok("All four Graph settings are present")
    note(f"mailbox: {settings.graph_quoting_mailbox}")
    note(f"category: {settings.graph_rfq_category or '(none — every message qualifies)'}")

    client = get_graph_client()

    # --- 2. token ---------------------------------------------------------
    try:
        client.token()
        ok("Signed in to Microsoft Graph")
    except GraphNotConfigured as exc:
        fail(str(exc), "Fill in the missing setting and run this again.")
        return 1
    except GraphError as exc:
        detail = str(exc)
        if "AADSTS7000215" in detail:
            fix = """
            The client secret is wrong. Note that Azure shows a secret's
            Value and its Secret ID — you need the Value, and it is only
            visible immediately after creating it. If you copied the ID,
            create a new secret and copy the Value this time.
            """
        elif "AADSTS700016" in detail or "not found in the directory" in detail:
            fix = """
            The client ID is not in this tenant. Check AQM_GRAPH_CLIENT_ID
            and AQM_GRAPH_TENANT_ID both came from the same app registration.
            """
        elif "AADSTS90002" in detail:
            fix = "The tenant ID does not exist. Check AQM_GRAPH_TENANT_ID."
        elif "AADSTS7000222" in detail or "expired" in detail.lower():
            fix = """
            The client secret has expired. Create a new one in the app
            registration under Certificates & secrets, and update
            AQM_GRAPH_CLIENT_SECRET.
            """
        else:
            fix = "Check the tenant ID, client ID and client secret."
        fail(f"Could not sign in: {detail[:400]}", fix)
        return 1

    # --- 3. mailbox -------------------------------------------------------
    try:
        mailbox = client.check_mailbox()
        ok(f"Found the mailbox: {mailbox.get('displayName')} <{mailbox.get('mail')}>")
    except GraphError as exc:
        detail = str(exc)
        if "403" in detail or "Access" in detail or "Authorization" in detail:
            fix = """
            Signed in, but not allowed to read this mailbox. The app
            registration needs the APPLICATION permissions Mail.Read and
            Mail.ReadWrite, and an admin must click "Grant admin consent" —
            adding them is not enough on its own.

            If an Application Access Policy is in place, check this mailbox
            is inside its scope.
            """
        elif "404" in detail:
            fix = f"""
            No mailbox at {settings.graph_quoting_mailbox}. Check the address,
            and that it is a mailbox rather than a distribution list — a
            distribution list has no mailbox to read.
            """
        else:
            fix = "Check the mailbox address."
        fail(f"Cannot read the mailbox: {detail[:400]}", fix)
        return 1

    # --- 4. category ------------------------------------------------------
    category_ok = True
    if settings.graph_rfq_category:
        try:
            categories = client.list_categories()
            if settings.graph_rfq_category in categories:
                ok(f"The '{settings.graph_rfq_category}' category exists in this mailbox")
            else:
                category_ok = False
                fail(
                    f"No category named '{settings.graph_rfq_category}' in this mailbox",
                    f"""
                    Nothing will ever be picked up until this is fixed, and it
                    will fail silently — mail arrives, nothing happens.

                    Either create the category in Outlook on the shared
                    mailbox, or set AQM_GRAPH_RFQ_CATEGORY to one that exists.

                    Categories found: {", ".join(categories) or "(none defined)"}

                    Note the name is case-sensitive: 'RFQ' and 'Rfq' are
                    different categories.
                    """,
                )
        except GraphError as exc:
            fail(f"Could not read the categories: {exc}", "Check the mailbox permissions.")

    # --- 5. tagged mail ---------------------------------------------------
    try:
        tagged = client.list_tagged_messages(limit=10)
        if tagged:
            ok(f"{len(tagged)} tagged message(s) waiting")
            for message in tagged[:5]:
                sender = (message.get("from") or {}).get("emailAddress", {}).get("address", "?")
                attachments = (
                    "with attachments" if message.get("hasAttachments") else "NO attachments"
                )
                note(f"{message.get('subject') or '(no subject)'} — {sender} — {attachments}")
        elif category_ok:
            ok("Connection works. No tagged mail waiting right now")
            note(
                f"Tag a message with '{settings.graph_rfq_category}' in the shared "
                "mailbox and run this again to see it appear."
            )
    except GraphError as exc:
        fail(f"Could not list tagged mail: {exc}", "Check the mailbox permissions.")
        return 1

    # --- 6. optional ingest ----------------------------------------------
    if args.poll:
        print()
        from app.db import SessionLocal, init_db
        from app.services.intake import poll_mailbox

        init_db()
        db = SessionLocal()
        try:
            result = poll_mailbox(db, client=client)
            db.commit()
            ok(
                f"Pulled {result.new_count} new enquir"
                f"{'y' if result.new_count == 1 else 'ies'} "
                f"({result.already_known} already known)"
            )
            for enquiry_id in result.ingested:
                note(f"enquiry {enquiry_id}")
            for failure in result.failed:
                fail(failure, "This message needs handling by hand.")
        finally:
            db.close()

    print("\nDone.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
