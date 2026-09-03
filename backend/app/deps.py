"""Request dependencies: identity and common lookups.

Approval, corrections and notes all record *who*. That only means anything if
the identity is real, so in production `AQM_AUTH_REQUIRED=true` makes a
validated Entra ID token mandatory and there is no header fallback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import Enquiry, Part, Quote

logger = logging.getLogger(__name__)


@dataclass
class CurrentUser:
    email: str
    name: str | None = None

    def __str__(self) -> str:
        return self.email


def _decode_entra_token(token: str, settings: Settings) -> CurrentUser:
    """Validate an Entra ID access token and return the caller.

    Signature, issuer, audience and expiry are all checked. A token that
    fails any of them is rejected — there is no "allow if it looks about
    right" path, because the name on an approval is the whole point.
    """
    try:
        import jwt
        from jwt import PyJWKClient
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="PyJWT is not installed but AQM_AUTH_REQUIRED is set",
        ) from exc

    if not settings.entra_tenant_id or not settings.entra_audience:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AQM_ENTRA_TENANT_ID and AQM_ENTRA_AUDIENCE must be set",
        )

    jwks_url = f"https://login.microsoftonline.com/{settings.entra_tenant_id}/discovery/v2.0/keys"
    try:
        signing_key = PyJWKClient(jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.entra_audience,
            issuer=f"https://login.microsoftonline.com/{settings.entra_tenant_id}/v2.0",
        )
    except Exception as exc:
        logger.warning("Rejected token: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        ) from exc

    email = claims.get("preferred_username") or claims.get("upn") or claims.get("email")
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token carries no user identity",
        )
    return CurrentUser(email=email, name=claims.get("name"))


def get_current_user(
    authorization: str | None = Header(default=None),
    x_user_email: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> CurrentUser:
    if settings.auth_required:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Entra ID bearer token required",
            )
        return _decode_entra_token(authorization.split(" ", 1)[1], settings)

    # Development only. Never reachable with AQM_AUTH_REQUIRED=true.
    return CurrentUser(email=x_user_email or "dev@localhost", name="Development user")


def get_ai(settings: Settings = Depends(get_settings)):
    """The AI client, as a dependency so tests can substitute a stub."""
    from app.services.ai import get_ai_client

    return get_ai_client(settings)


def get_storage_dep(settings: Settings = Depends(get_settings)):
    """Attachment storage, as a dependency for the same reason."""
    from app.services.storage import get_storage

    return get_storage(settings)


def get_enquiry(enquiry_id: int, db: Session = Depends(get_db)) -> Enquiry:
    enquiry = db.get(Enquiry, enquiry_id)
    if enquiry is None:
        raise HTTPException(status_code=404, detail=f"Enquiry {enquiry_id} not found")
    return enquiry


def get_part(part_id: int, db: Session = Depends(get_db)) -> Part:
    part = db.get(Part, part_id)
    if part is None:
        raise HTTPException(status_code=404, detail=f"Part {part_id} not found")
    return part


def get_quote(quote_id: int, db: Session = Depends(get_db)) -> Quote:
    quote = db.get(Quote, quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail=f"Quote {quote_id} not found")
    return quote
