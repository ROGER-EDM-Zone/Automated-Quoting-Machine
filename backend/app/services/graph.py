"""Microsoft Graph client (spec stage 1 and 6).

Two jobs: receive tagged mail from the shared quoting mailbox, and put a draft
reply into the estimator's mailbox. It never sends anything — `createReply`
leaves the message in Drafts and a human presses send.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
LOGIN_ROOT = "https://login.microsoftonline.com"

#: Graph caps mail subscriptions at roughly three days; renew well inside that.
SUBSCRIPTION_MINUTES = 4230


class GraphError(Exception):
    pass


class GraphNotConfigured(GraphError):
    pass


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
        missing = [
            name
            for name, value in (
                ("AQM_GRAPH_TENANT_ID", self.settings.graph_tenant_id),
                ("AQM_GRAPH_CLIENT_ID", self.settings.graph_client_id),
                ("AQM_GRAPH_CLIENT_SECRET", self.settings.graph_client_secret),
                ("AQM_GRAPH_QUOTING_MAILBOX", self.settings.graph_quoting_mailbox),
            )
            if not value
        ]
        if missing:
            raise GraphNotConfigured(f"Graph is not configured: {', '.join(missing)} unset")

    def token(self) -> str:
        self._require_config()
        now = datetime.now(UTC)
        if self._token and self._token_expires and now < self._token_expires:
            return self._token

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
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expires = now + timedelta(seconds=payload.get("expires_in", 3600) - 120)
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
