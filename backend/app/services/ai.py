"""Anthropic client wrapper.

Every AI call in this system goes through here, and every one of them is
constrained to a JSON schema. That is deliberate: the AI produces *inputs* —
structured fields, classifications, proposed changes — and never prose that
some downstream code then has to parse for a number.

What this module will not do:
  * return a partial or guessed result when the model declines or errors — it
    raises, and the caller turns that into a flag for a human;
  * accept a value the schema did not ask for.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class AIError(Exception):
    """Base class for AI-call failures. Always surfaced, never swallowed."""


class AIUnavailable(AIError):
    """No API key configured, or the SDK is not installed."""


class AIRefused(AIError):
    """The model declined the request (stop_reason == 'refusal').

    Treated as "no answer", which becomes a needs_attention flag. It is never
    treated as an empty or default answer.
    """

    def __init__(self, category: str | None, explanation: str | None) -> None:
        self.category = category
        self.explanation = explanation
        super().__init__(f"Model declined the request ({category or 'unspecified'})")


class AIMalformedResponse(AIError):
    """The response was not the JSON the schema demanded."""


@dataclass
class ImageBlock:
    """One rasterised drawing page, ready to send."""

    base64_data: str
    media_type: str = "image/png"
    #: Human label used in the accompanying text, e.g. "page 2 of 3".
    label: str | None = None


class StructuredCaller(Protocol):
    """The seam the services depend on, so tests can inject a stub."""

    def structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        images: list[ImageBlock] | None = ...,
        effort: str = ...,
    ) -> dict[str, Any]: ...


class AnthropicAIClient:
    """Thin wrapper over the Messages API constrained to a JSON schema."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = None

    # -- lifecycle ------------------------------------------------------
    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self.settings.anthropic_api_key:
            raise AIUnavailable(
                "AQM_ANTHROPIC_API_KEY is not set — extraction and "
                "classification cannot run"
            )
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise AIUnavailable("the `anthropic` package is not installed") from exc
        self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        return self._client

    # -- the one call ---------------------------------------------------
    def structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        images: list[ImageBlock] | None = None,
        effort: str = "high",
    ) -> dict[str, Any]:
        """Make one schema-constrained call and return the parsed object.

        ``output_config.format`` guarantees the response's first text block is
        JSON matching ``schema``, so there is no prose to parse and no place
        for the model to volunteer an unrequested field.
        """
        client = self._ensure_client()

        content: list[dict[str, Any]] = []
        for image in images or []:
            if image.label:
                content.append({"type": "text", "text": image.label})
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image.media_type,
                        "data": image.base64_data,
                    },
                }
            )
        content.append({"type": "text", "text": prompt})

        request: dict[str, Any] = {
            "model": self.settings.anthropic_model,
            "max_tokens": self.settings.anthropic_max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": content}],
            "thinking": {"type": "adaptive"},
            "output_config": {
                "effort": effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        }

        # A policy decline would otherwise silently end the turn. Routing the
        # request to a fallback model keeps a drawing from stalling the queue;
        # if the whole chain declines we still raise rather than invent data.
        if self.settings.anthropic_refusal_fallback:
            response = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                **request,
            )
        else:
            response = client.messages.create(**request)

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise AIRefused(
                category=getattr(details, "category", None),
                explanation=getattr(details, "explanation", None),
            )
        if response.stop_reason == "max_tokens":
            raise AIMalformedResponse(
                "Response hit max_tokens before the JSON was complete; "
                "raise AQM_ANTHROPIC_MAX_TOKENS or split the request"
            )

        return self._parse(response)

    @staticmethod
    def _parse(response) -> dict[str, Any]:
        text = next(
            (block.text for block in response.content if block.type == "text"), None
        )
        if not text:
            raise AIMalformedResponse("Response contained no text block")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AIMalformedResponse(f"Response was not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise AIMalformedResponse("Response JSON was not an object")
        return payload


class StubAIClient:
    """Test double. Returns queued payloads and records what it was asked.

    Also used to run the pipeline end to end in development without an API
    key, so the deterministic half of the system can be exercised on its own.
    """

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        images: list[ImageBlock] | None = None,
        effort: str = "high",
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "system": system,
                "prompt": prompt,
                "schema": schema,
                "images": list(images or []),
                "effort": effort,
            }
        )
        if not self.responses:
            raise AIUnavailable("StubAIClient has no queued responses left")
        return self.responses.pop(0)


def get_ai_client(settings: Settings | None = None) -> StructuredCaller:
    return AnthropicAIClient(settings)
