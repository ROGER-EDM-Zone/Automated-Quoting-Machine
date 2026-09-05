"""Sign the app in to the mailbox, once, in a browser.

    python -m scripts.sign_in

This is the light way to connect the mailbox. Instead of the app holding an
identity of its own — which an administrator has to create and consent to —
a person signs in once and the app keeps the resulting token.

What that buys:

  * No administrator and no consent screen. The person signing in is only
    granting access to what they can already see.
  * **No client secret anywhere.** Nothing has to be emailed, pasted into a
    chat, or kept in a password manager, because no password for the app
    exists to begin with.
  * The app can never see more than the person who signed in. If they can
    only read the sales mailbox, neither can it.

What it costs: the sign-in has to be repeated if the app sits unused for
months, and it acts as that person rather than as itself. For getting real
RFQs flowing and judging whether the extraction is any good, that is a fair
trade. Move to `AQM_GRAPH_AUTH_MODE=app` for unattended production running.

Before running this you need an app registration — but a much simpler one
than app mode needs, and one a non-administrator can usually make:

    1. entra.microsoft.com -> App registrations -> New registration
    2. Name it, choose "Accounts in this organizational directory only",
       leave the redirect URI blank, Register
    3. Copy the Application (client) ID and Directory (tenant) ID
    4. Authentication -> Advanced settings ->
       "Allow public client flows" -> YES        <- easy to miss
    5. API permissions -> Microsoft Graph -> DELEGATED permissions ->
       Mail.Read and Mail.ReadWrite

No client secret. No admin consent — the person signing in consents for
themselves the first time.

Then in backend/.env:

    AQM_GRAPH_AUTH_MODE=user
    AQM_GRAPH_TENANT_ID=...
    AQM_GRAPH_CLIENT_ID=...
    AQM_GRAPH_QUOTING_MAILBOX=sales@edmzone.co.uk
"""

from __future__ import annotations

import sys

from app.config import get_settings
from app.services.graph import GraphClient, GraphError, GraphNotConfigured

TICK = "  \033[32m✓\033[0m"
CROSS = "  \033[31m✗\033[0m"


def main() -> int:
    settings = get_settings()

    if settings.graph_auth_mode != "user":
        print(
            f"\nAQM_GRAPH_AUTH_MODE is '{settings.graph_auth_mode}', not 'user'.\n\n"
            "This script is for the sign-in-once mode. In 'app' mode the app\n"
            "has its own identity and needs no sign-in — check it instead with:\n"
            "    python -m scripts.check_graph\n",
            file=sys.stderr,
        )
        return 1

    client = GraphClient(settings)

    try:
        device = client.begin_device_login()
    except GraphNotConfigured as exc:
        print(f"\n{CROSS} {exc}\n", file=sys.stderr)
        return 1
    except GraphError as exc:
        detail = str(exc)
        print(f"\n{CROSS} {detail}\n", file=sys.stderr)
        if "unauthorized_client" in detail or "public client" in detail.lower():
            print(
                "      This usually means 'Allow public client flows' is still\n"
                "      off. Turn it on: entra.microsoft.com -> your app ->\n"
                "      Authentication -> Advanced settings.\n",
                file=sys.stderr,
            )
        return 1

    print("\n" + "=" * 66)
    print("  Open this page and enter the code:\n")
    print(f"      {device.get('verification_uri')}")
    print(f"      code:  {device.get('user_code')}\n")
    print(f"  Sign in as {settings.graph_quoting_mailbox} — or as somebody")
    print("  who can open that mailbox.")
    print("=" * 66)
    print("\nWaiting for you to finish in the browser...\n")

    try:
        client.complete_device_login(device)
    except GraphError as exc:
        print(f"{CROSS} {exc}\n", file=sys.stderr)
        return 1

    print(f"{TICK} Signed in. The app can now read the mailbox on its own.")
    print(f"      Token kept in {settings.graph_token_cache} — treat it as a password.\n")

    try:
        mailbox = client.check_mailbox()
        print(f"{TICK} Reading: {mailbox.get('displayName')} <{mailbox.get('mail')}>")
    except GraphError as exc:
        print(f"{CROSS} Signed in, but cannot read the mailbox: {exc}", file=sys.stderr)
        print(
            "      If you signed in as yourself rather than as the sales\n"
            "      account, you need permission to open that mailbox.\n",
            file=sys.stderr,
        )
        return 1

    print("\nNow run:  python -m scripts.check_graph --poll\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
