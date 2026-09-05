"""Microsoft Graph client (spec stage 1 and 6).

Two jobs: receive tagged mail from the shared quoting mailbox, and put a draft
reply into the estimator's mailbox. It never sends anything — `createReply`
leaves the message in Drafts and a human presses send.

There are two ways it can prove who it is, and which one to use is a question
about the business rather than about the code:

``app`` mode
    The app holds an identity of its own and works unattended forever. It
    needs an administrator to register it and consent to *application*
    permissions, and those permissions reach every mailbox in the tenant
    unless somebody scopes them. This is the production answer.

``user`` mode
    A person signs in once, in a browser, and the app keeps the token that
    results. No administrator, no consent screen, and — the part that matters
    most — no client secret in existence to be emailed around or leaked. The
    app can see exactly what that person can see and nothing else, which is
    its own kind of safety. The trade is that the sign-in has to be repeated
    if the app goes unused for months.

Both modes talk to the same endpoints and return the same objects, so nothing
downstream knows or cares which is in use.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
LOGIN_ROOT = "https://login.microsoftonline.com"

#: Graph caps mail subscriptions at roughly three days; renew well inside that.
SUBSCRIPTION_MINUTES = 4230

#: What `user` mode asks for. `offline_access` is what makes the sign-in last
#: beyond the hour an access token lives — without it the app would need
#: somebody at a keyboard every hour, which is not automation.
USER_SCOPES = (
    "https://graph.microsoft.com/Mail.Read "
    "https://graph.microsoft.com/Mail.ReadWrite "
    "offline_access"
)


class GraphError(Exception):
    pass


class GraphNotConfigured(GraphError):
    pass


class GraphNeedsSignIn(GraphError):
    """`user` mode with no usable token. A person has to sign in again."""


@dataclass
class GraphAttachment:
    filename: str
    content_type: str | None
    content_bytes: bytes


@dataclass
class GraphMessage:
    message_id: str
    subject: str | None
    body_text: str | None
    sender_email: str | None
    sender_name: str | None
    received_at: datetime | None
    categories: list[str]
    attachments: list[GraphAttachment]


class GraphClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._token: str | None = None
        self._token_expires: datetime | None = None

    # -- auth -----------------------------------------------------------
    def _require_config(self) -> None:
        required = [
            ("AQM_GRAPH_TENANT_ID", self.settings.graph_tenant_id),
            ("AQM_GRAPH_CLIENT_ID", self.settings.graph_client_id),
            ("AQM_GRAPH_QUOTING_MAILBOX", self.settings.graph_quoting_mailbox),
        ]
        # In `user` mode there is no client secret and there is not meant to
        # be one: a public client that holds a secret is a public client with
        # a leak waiting to happen.
        if not self.is_user_mode:
            required.append(("AQM_GRAPH_CLIENT_SECRET", self.settings.graph_client_secret))

        missing = [name for name, value in required if not value]
        if missing:
            raise GraphNotConfigured(f"Graph is not configured: {', '.join(missing)} unset")

    @property
    def is_user_mode(self) -> bool:
        return self.settings.graph_auth_mode == "user"

    def token(self) -> str:
        self._require_config()
        now = datetime.now(UTC)
        if self._token and self._token_expires and now < self._token_expires:
            return self._token
        return self._sign_in_as_user() if self.is_user_mode else self._sign_in_as_app()

    # -- app mode: the app has its own identity ---------------------------
    def _sign_in_as_app(self) -> str:
        response = httpx.post(
            f"{LOGIN_ROOT}/{self.settings.graph_tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": self.settings.graph_client_id,
                "client_secret": self.settings.graph_client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
            timeout=30,
        )
        if response.status_code != 200:
            raise GraphError(f"Token request failed: {response.status_code} {response.text}")
        return self._store_token(response.json())

    # -- user mode: somebody signed in once -------------------------------
    def _token_cache_path(self) -> Path:
        return Path(self.settings.graph_token_cache)

    def _read_refresh_token(self) -> str | None:
        path = self._token_cache_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text()).get("refresh_token")
        except (json.JSONDecodeError, OSError):
            return None

    def _write_refresh_token(self, payload: dict[str, Any]) -> None:
        """Keep the refresh token, and only that, readable by nobody else.

        A refresh token is a standing key to the mailbox, so it is written the
        way a private key is: 0600, and never anywhere near the repository.
        """
        refresh = payload.get("refresh_token")
        if not refresh:
            return
        path = self._token_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "refresh_token": refresh,
                    "signed_in_at": datetime.now(UTC).isoformat(),
                    "tenant": self.settings.graph_tenant_id,
                    "client_id": self.settings.graph_client_id,
                }
            )
        )
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover - Windows and odd filesystems
            logger.warning("Could not restrict permissions on %s", path)

    def _sign_in_as_user(self) -> str:
        refresh = self._read_refresh_token()
        if not refresh:
            raise GraphNeedsSignIn("Nobody has signed in yet. Run: python -m scripts.sign_in")

        response = httpx.post(
            f"{LOGIN_ROOT}/{self.settings.graph_tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": self.settings.graph_client_id,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
                "scope": USER_SCOPES,
            },
            timeout=30,
        )
        if response.status_code != 200:
            raise GraphNeedsSignIn(
                "The saved sign-in is no longer valid — the password may have "
                "changed, or it has gone unused too long. "
                "Run: python -m scripts.sign_in\n"
                f"({response.status_code} {response.text[:300]})"
            )
        payload = response.json()
        # Microsoft rotates the refresh token on each use; keeping the new one
        # is what makes the sign-in last indefinitely rather than 90 days.
        self._write_refresh_token(payload)
        return self._store_token(payload)

    def begin_device_login(self) -> dict[str, Any]:
        """Ask Microsoft for a code the person types into their browser."""
        self._require_config()
        response = httpx.post(
            f"{LOGIN_ROOT}/{self.settings.graph_tenant_id}/oauth2/v2.0/devicecode",
            data={"client_id": self.settings.graph_client_id, "scope": USER_SCOPES},
            timeout=30,
        )
        if response.status_code != 200:
            raise GraphError(
                f"Could not start sign-in: {response.status_code} {response.text[:400]}"
            )
        return response.json()

    def complete_device_login(self, device: dict[str, Any]) -> str:
        """Wait for the person to finish signing in, then keep the token."""
        interval = int(device.get("interval", 5))
        deadline = time.monotonic() + int(device.get("expires_in", 900))

        while time.monotonic() < deadline:
            time.sleep(interval)
            response = httpx.post(
                f"{LOGIN_ROOT}/{self.settings.graph_tenant_id}/oauth2/v2.0/token",
                data={
                    "client_id": self.settings.graph_client_id,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device["device_code"],
                },
                timeout=30,
            )
            if response.status_code == 200:
                payload = response.json()
                self._write_refresh_token(payload)
                return self._store_token(payload)

            error = response.json().get("error", "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error == "expired_token":
                raise GraphError("The sign-in code expired. Start again.")
            if error == "authorization_declined":
                raise GraphError("Sign-in was declined in the browser.")
            raise GraphError(f"Sign-in failed: {response.text[:400]}")

        raise GraphError("Timed out waiting for the sign-in to be completed.")

    def _store_token(self, payload: dict[str, Any]) -> str:
        self._token = payload["access_token"]
        self._token_expires = datetime.now(UTC) + timedelta(
            seconds=payload.get("expires_in", 3600) - 120
        )
        return self._token

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = httpx.request(
            method,
            f"{GRAPH_ROOT}{path}",
            headers={"Authorization": f"Bearer {self.token()}"},
            timeout=60,
            **kwargs,
        )
        if response.status_code >= 400:
            raise GraphError(f"{method} {path} failed: {response.status_code} {response.text}")
        return response

    # -- intake ---------------------------------------------------------
    def create_subscription(self, notification_url: str) -> dict[str, Any]:
        """Subscribe to new mail in the quoting mailbox."""
        expiry = datetime.now(UTC) + timedelta(minutes=SUBSCRIPTION_MINUTES)
        body = {
            "changeType": "created",
            "notificationUrl": notification_url,
            "resource": f"users/{self.settings.graph_quoting_mailbox}/mailFolders('Inbox')/messages",
            "expirationDateTime": expiry.isoformat().replace("+00:00", "Z"),
            "clientState": self.settings.graph_webhook_client_state or "",
        }
        return self._request("POST", "/subscriptions", json=body).json()

    def renew_subscription(self, subscription_id: str) -> dict[str, Any]:
        expiry = datetime.now(UTC) + timedelta(minutes=SUBSCRIPTION_MINUTES)
        return self._request(
            "PATCH",
            f"/subscriptions/{subscription_id}",
            json={"expirationDateTime": expiry.isoformat().replace("+00:00", "Z")},
        ).json()

    def check_mailbox(self) -> dict[str, Any]:
        """Confirm the mailbox exists and we can see it.

        Used by the connection check so a misconfiguration reports as "the
        mailbox is wrong" rather than as a mysterious failure three steps later.
        """
        mailbox = self.settings.graph_quoting_mailbox
        return self._request(
            "GET",
            f"/users/{mailbox}",
            params={"$select": "id,displayName,mail,userPrincipalName"},
        ).json()

    def list_tagged_messages(
        self,
        *,
        category: str | None = None,
        limit: int = 25,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Recent Inbox messages carrying the RFQ category.

        Filtering server-side keeps the shop's ordinary mail out of the app
        entirely — untagged messages are never fetched, never stored, and never
        read by the AI.
        """
        mailbox = self.settings.graph_quoting_mailbox
        category = category if category is not None else self.settings.graph_rfq_category

        filters: list[str] = []
        if category:
            escaped = category.replace("'", "''")
            filters.append(f"categories/any(c:c eq '{escaped}')")
        if since is not None:
            filters.append(f"receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}")

        params: dict[str, Any] = {
            "$select": "id,subject,receivedDateTime,categories,hasAttachments,from",
            "$orderby": "receivedDateTime desc",
            "$top": max(1, min(limit, 100)),
        }
        if filters:
            params["$filter"] = " and ".join(filters)

        response = self._request(
            "GET",
            f"/users/{mailbox}/mailFolders('Inbox')/messages",
            params=params,
        )
        return response.json().get("value", [])

    def list_categories(self) -> list[str]:
        """The categories defined on the mailbox.

        Lets the connection check say "the category you configured does not
        exist in this mailbox" — by far the most likely reason for a silent
        pipeline, and invisible otherwise.
        """
        mailbox = self.settings.graph_quoting_mailbox
        data = self._request("GET", f"/users/{mailbox}/outlook/masterCategories").json()
        return [entry.get("displayName", "") for entry in data.get("value", [])]

    def get_message(self, message_id: str) -> GraphMessage:
        mailbox = self.settings.graph_quoting_mailbox
        data = self._request(
            "GET",
            f"/users/{mailbox}/messages/{message_id}",
            params={
                "$select": "id,subject,body,bodyPreview,from,receivedDateTime,categories,hasAttachments"
            },
        ).json()

        attachments: list[GraphAttachment] = []
        if data.get("hasAttachments"):
            attachments = self.get_attachments(message_id)

        sender = (data.get("from") or {}).get("emailAddress") or {}
        received = data.get("receivedDateTime")
        return GraphMessage(
            message_id=data["id"],
            subject=data.get("subject"),
            body_text=_body_text(data),
            sender_email=sender.get("address"),
            sender_name=sender.get("name"),
            received_at=(
                datetime.fromisoformat(received.replace("Z", "+00:00")) if received else None
            ),
            categories=data.get("categories") or [],
            attachments=attachments,
        )

    def get_attachments(self, message_id: str) -> list[GraphAttachment]:
        import base64

        mailbox = self.settings.graph_quoting_mailbox
        data = self._request("GET", f"/users/{mailbox}/messages/{message_id}/attachments").json()
        results: list[GraphAttachment] = []
        for item in data.get("value", []):
            if item.get("@odata.type") != "#microsoft.graph.fileAttachment":
                # Item and reference attachments (linked OneDrive files) need
                # separate handling; skipped rather than half-read.
                logger.info("Skipping non-file attachment %s", item.get("name"))
                continue
            results.append(
                GraphAttachment(
                    filename=item.get("name") or "attachment",
                    content_type=item.get("contentType"),
                    content_bytes=base64.b64decode(item.get("contentBytes") or b""),
                )
            )
        return results

    # -- reply ----------------------------------------------------------
    def create_draft_reply(
        self,
        *,
        message_id: str,
        mailbox: str,
        subject: str,
        body_html: str,
    ) -> dict[str, Any]:
        """Create a reply draft. Deliberately does not send it."""
        draft = self._request("POST", f"/users/{mailbox}/messages/{message_id}/createReply").json()
        draft_id = draft["id"]
        self._request(
            "PATCH",
            f"/users/{mailbox}/messages/{draft_id}",
            json={
                "subject": subject,
                "body": {"contentType": "HTML", "content": body_html},
            },
        )
        return {"draft_id": draft_id, "web_link": draft.get("webLink")}


def _body_text(data: dict[str, Any]) -> str | None:
    body = data.get("body") or {}
    content = body.get("content")
    if not content:
        return data.get("bodyPreview")
    if (body.get("contentType") or "").lower() == "html":
        return _strip_html(content)
    return content


def _strip_html(html: str) -> str:
    """Crude HTML-to-text for the stored body.

    Good enough for a model to read and for regex reference-matching. The
    original message stays in Outlook if anyone needs the formatting.
    """
    import re
    from html import unescape

    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def get_graph_client(settings: Settings | None = None) -> GraphClient:
    return GraphClient(settings)
