"""Drawing-extraction prompt and its JSON schema (spec stage 2).

The whole prompt is built around one instruction: return null rather than a
guess. An honest blank beats a wrong number that flows silently into pricing.
The confidence score is what makes that instruction enforceable — a field the
model is unsure of gets withheld by `services.confidence` before it can reach
the pricing engine.
"""

from __future__ import annotations

from typing import Any

SYSTEM = """\
You are reading an engineering drawing for a precision subcontract machine \
shop, so that an estimator can quote it. You do not quote, price, or estimate \
times — you only report what the drawing says.

Rules you must follow exactly:

1. Report only what you can actually read on the drawing. If a field is not \
legible, not present, or you are inferring it from context rather than reading \
it, return null for that field. A null is the correct, useful answer. A \
plausible-looking guess is the worst possible answer, because it will be \
priced as fact.

2. Give an honest confidence between 0 and 1 for every field you return a \
value for. Confidence means "how sure am I that this is what the drawing \
says", not "how likely is this to be right in general". Do not inflate it. If \
you are reading a partly obscured or hand-annotated dimension, say so in the \
confidence and in the evidence.

3. Never reconcile a disagreement yourself. If two views, a table and a note, \
or a dimension and a general tolerance conflict, return the value you consider \
best supported, lower the confidence, and describe the conflict in \
`conflicts`. The estimator resolves it, not you.

4. Do not convert units. Report the number and the unit as drawn. If the \
drawing is in inches, say inches.

5. Tolerances: report the single tightest tolerance on the drawing, as written \
(e.g. "+0.000/-0.013", "H7", "±0.05"). If the only tolerance is a general \
block tolerance, report that and note it in the evidence.

6. Do not list operations, processes, machines, or cycle times. That is not \
your job and anything you invent there is unusable.
"""

#: Fields the vision call reports, each as {value, confidence, evidence}.
FIELDS: tuple[str, ...] = (
    "drawing_number",
    "revision",
    "description",
    "quantity",
    "material",
    "heat_treatment",
    "surface_coat",
    "finish_spec",
    "envelope_x",
    "envelope_y",
    "envelope_z",
    "tightest_tolerance",
)

_FIELD_GUIDANCE: dict[str, str] = {
    "drawing_number": "The drawing or part number from the title block.",
    "revision": "The revision letter or number currently in force.",
    "description": "The part name or description from the title block.",
    "quantity": (
        "Quantity required, if the drawing states one. Drawings often do not — "
        "return null rather than assuming 1."
    ),
    "material": "Material specification exactly as written, e.g. '1.2312', 'EN24T'.",
    "heat_treatment": "Heat treatment or hardness requirement, e.g. '52-54 HRC'.",
    "surface_coat": "Plating, coating or surface treatment called for.",
    "finish_spec": "Surface finish requirement, e.g. 'Ra 0.8'.",
    "envelope_x": "Largest overall dimension, as drawn.",
    "envelope_y": "Second overall dimension, as drawn.",
    "envelope_z": "Third overall dimension (thickness or length), as drawn.",
    "tightest_tolerance": "The single tightest tolerance on the drawing, as written.",
}

_NUMERIC_FIELDS = frozenset({"envelope_x", "envelope_y", "envelope_z"})


def _field_schema(name: str) -> dict[str, Any]:
    if name == "quantity":
        value_schema: dict[str, Any] = {"type": ["integer", "null"]}
    elif name in _NUMERIC_FIELDS:
        value_schema = {"type": ["number", "null"]}
    else:
        value_schema = {"type": ["string", "null"]}
    return {
        "type": "object",
        "description": _FIELD_GUIDANCE[name],
        "properties": {
            "value": value_schema,
            "confidence": {
                "type": ["number", "null"],
                "minimum": 0,
                "maximum": 1,
                "description": (
                    "How sure you are that this is what the drawing says. "
                    "Null when value is null."
                ),
            },
            "evidence": {
                "type": ["string", "null"],
                "description": (
                    "Where on the drawing you read this, and any caveat. "
                    "One short phrase."
                ),
            },
        },
        "required": ["value", "confidence", "evidence"],
        "additionalProperties": False,
    }


def build_schema() -> dict[str, Any]:
    """The forced output schema for one drawing."""
    return {
        "type": "object",
        "properties": {
            **{name: _field_schema(name) for name in FIELDS},
            "units": {
                "type": ["string", "null"],
                "description": "Drawing units as stated, e.g. 'mm' or 'inch'.",
            },
            "features": {
                "type": "object",
                "description": (
                    "Countable features that drive machining, as read. Omit "
                    "any you cannot count."
                ),
                "properties": {
                    "holes": {"type": ["integer", "null"]},
                    "tapped_holes": {"type": ["integer", "null"]},
                    "counterbores": {"type": ["integer", "null"]},
                    "pockets": {"type": ["integer", "null"]},
                    "slots": {"type": ["integer", "null"]},
                    "internal_corners_below_1mm_radius": {"type": ["integer", "null"]},
                    "through_wire_starts": {"type": ["integer", "null"]},
                    "notes": {
                        "type": ["string", "null"],
                        "description": (
                            "Anything on the drawing an estimator would want "
                            "flagged: thin walls, deep narrow pockets, "
                            "sharp internal corners, awkward datums."
                        ),
                    },
                },
                "required": [
                    "holes",
                    "tapped_holes",
                    "counterbores",
                    "pockets",
                    "slots",
                    "internal_corners_below_1mm_radius",
                    "through_wire_starts",
                    "notes",
                ],
                "additionalProperties": False,
            },
            "conflicts": {
                "type": "array",
                "description": (
                    "Disagreements you found and did NOT resolve. One entry "
                    "per conflict."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "detail": {"type": "string"},
                    },
                    "required": ["field", "detail"],
                    "additionalProperties": False,
                },
            },
            "illegible": {
                "type": "array",
                "description": (
                    "Fields you could not read at all, and why (poor scan, "
                    "cropped view, handwritten)."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["field", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": [*FIELDS, "units", "features", "conflicts", "illegible"],
        "additionalProperties": False,
    }


def build_prompt(
    *,
    page_count: int,
    filename: str,
    email_subject: str | None = None,
    email_body: str | None = None,
) -> str:
    """Assemble the user turn for one drawing.

    The email is included because it often carries the quantity and the
    material-supplied question that the drawing does not — but the model is
    told which source each value came from so `confidence.cross_check` can do
    its job instead of the model quietly picking a winner.
    """
    parts = [
        f"Drawing file: {filename} ({page_count} page(s) attached as images above).",
        "",
        "Read the drawing and fill in the schema. Remember: null for anything "
        "you cannot actually read, and an honest confidence for everything "
        "you can.",
    ]
    if email_subject or email_body:
        parts += [
            "",
            "For context only, the enquiry email that arrived with this "
            "drawing is quoted below. Use it to understand what is being "
            "asked for, but do NOT take field values from it — this schema "
            "records what the DRAWING says. If the email contradicts the "
            "drawing, record it under `conflicts`.",
            "",
            "--- enquiry email ---",
            f"Subject: {email_subject or '(none)'}",
            (email_body or "(no body)").strip()[:4000],
            "--- end of email ---",
        ]
    return "\n".join(parts)
