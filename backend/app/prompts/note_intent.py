"""Note-interpretation prompt and schema (spec stage 5).

An estimator types a sentence of context the AI could not have known — "we
already have the electrode for this", "they always chase us on this one, add
some slack". This call turns that sentence into a concrete change to the
*inputs*, which the deterministic engine then reprices.

The hard rule, enforced both here and in code: the model never chooses a
percentage. Adjustment sizes come from rules_table. If no rule fits, it asks.
"""

from __future__ import annotations

from typing import Any

from app.enums import Process

_PROCESSES = sorted(p.value for p in Process)

_PART_FIELDS = [
    "material",
    "heat_treatment",
    "surface_coat",
    "finish_spec",
    "tightest_tolerance",
    "quantity",
    "job_type",
    "description",
]

SYSTEM = """\
You are helping an estimator adjust a quote that has already been costed. They \
have written a note. Your job is to turn that note into concrete changes to \
the quote's inputs.

You never calculate or state a price. A deterministic engine recalculates the \
price from the inputs you change. If you find yourself wanting to say what the \
new price should be, you have misunderstood your role — change the input that \
produces it instead.

Classify the note first:

* fact_correction — the note tells you something about the job that changes \
the calculation. "We already have the electrode" removes a setup. "It's the \
same fixture as the last one" removes fixture time. "They're supplying the \
material" changes the job type.
* commercial_instruction — the note overrides a number for business reasons \
rather than technical ones. "Do this one keenly", "add contingency, this \
customer always changes the drawing", "they need it Friday".

Then propose actions. Rules for actions:

1. Only use a number the estimator actually gave you. If they said "take off \
the 15 minutes of electrode setup", 15 is theirs and you may use it. If they \
said "that setup time looks high", you do not know the new number — ask.

2. For any percentage adjustment — rush, contingency, discount, uplift — you \
must cite a rule from the list of available rules you were given, by its \
rule_key. You may not invent a percentage, and you may not pick one that \
"seems about right". If no rule in the list fits what they asked for, use the \
`ask` action and say what rule would be needed. A percentage nobody in the \
business agreed is exactly what this system exists to prevent.

3. One action per distinct change. If the note does two things, return two \
actions.

4. If the note is just an observation with no change implied — "customer is \
usually slow to respond" — return no actions and say so in the summary. That \
is a perfectly good outcome; the note is still worth recording.

5. If you are unsure what they meant, ask. An `ask` action costs the estimator \
ten seconds. A wrong action costs them a wrong quote they might not catch.
"""


def build_schema(rule_keys: list[str]) -> dict[str, Any]:
    """Schema for the note interpretation.

    `rule_keys` is the list of rules currently active in rules_table. It is
    baked into the schema as an enum so a rule the business has not defined is
    not merely discouraged — it is unrepresentable.
    """
    apply_rule_schema: dict[str, Any] = (
        {"type": ["string", "null"], "enum": [*rule_keys, None]}
        if rule_keys
        else {"type": "null"}
    )

    return {
        "type": "object",
        "properties": {
            "note_kind": {
                "type": "string",
                "enum": ["fact_correction", "commercial_instruction"],
            },
            "summary": {
                "type": "string",
                "description": (
                    "One line, in the estimator's terms, describing what you "
                    "changed and why. e.g. 'removed 15m electrode setup'."
                ),
            },
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "set_operation_time",
                                "remove_operation",
                                "add_operation",
                                "set_part_field",
                                "set_margin_pct",
                                "apply_rule",
                                "ask",
                            ],
                        },
                        "part_id": {"type": ["integer", "null"]},
                        "op_number": {"type": ["integer", "null"]},
                        "process": {"type": ["string", "null"], "enum": [*_PROCESSES, None]},
                        "description": {"type": ["string", "null"]},
                        "set_time_mins": {
                            "type": ["number", "null"],
                            "description": "Only if the estimator gave this number.",
                        },
                        "run_time_mins_per_unit": {
                            "type": ["number", "null"],
                            "description": "Only if the estimator gave this number.",
                        },
                        "field_name": {"type": ["string", "null"], "enum": [*_PART_FIELDS, None]},
                        "field_value": {"type": ["string", "null"]},
                        "margin_pct": {
                            "type": ["number", "null"],
                            "description": (
                                "Only when the estimator stated a margin "
                                "percentage themselves."
                            ),
                        },
                        "rule_key": apply_rule_schema,
                        "question": {
                            "type": ["string", "null"],
                            "description": "For the `ask` action: what you need to know.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Why this action follows from the note.",
                        },
                    },
                    "required": [
                        "action",
                        "part_id",
                        "op_number",
                        "process",
                        "description",
                        "set_time_mins",
                        "run_time_mins_per_unit",
                        "field_name",
                        "field_value",
                        "margin_pct",
                        "rule_key",
                        "question",
                        "reason",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["note_kind", "summary", "actions"],
        "additionalProperties": False,
    }


def build_prompt(*, note_text: str, quote_summary: str, rules_summary: str) -> str:
    return "\n".join(
        [
            "--- the estimator's note ---",
            note_text.strip(),
            "--- end of note ---",
            "",
            "--- the quote as it currently stands ---",
            quote_summary,
            "--- end of quote ---",
            "",
            "--- adjustment rules the business has defined ---",
            rules_summary,
            "(These are the ONLY percentages available to you. If none fits, ask.)",
            "--- end of rules ---",
            "",
            "Interpret the note against the schema.",
        ]
    )
