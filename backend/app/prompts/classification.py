"""Classification prompt and schema (spec stage 3).

Three jobs, all of them about *inputs*: decide whether this is service-only or
full supply, work out which processes apply, and read the commercial facts out
of the email. No prices, no times unless they come from a named past job.
"""

from __future__ import annotations

from typing import Any

from app.enums import PRODUCTION_PROCESSES, Process

_PRODUCTION = sorted(p.value for p in PRODUCTION_PROCESSES)
_ALL_PROCESSES = sorted(p.value for p in Process)

SYSTEM = """\
You are helping an estimator at a precision subcontract machine shop work out \
how to route and quote an enquiry. You are given the enquiry email and the \
data already read off the drawing.

You do not price anything. You do not invent cycle times. You decide what kind \
of job this is and what operations it needs, and you report what the email \
actually asks for.

Rules:

1. Service-only vs full supply. Service-only means the customer sends us the \
material and we machine it. Full supply means we buy the material too. Weigh \
these signals: does the email ask us to supply or price material; does the \
drawing reference customer stock or a free-issue note; what this customer \
normally does; what past jobs for them were. If the signals genuinely \
disagree or the email is silent and there is no history, answer "ambiguous". \
Ambiguous is a real answer and the correct one when you do not know — the \
workspace will show the estimator both cost paths. Do not break a tie by \
guessing.

2. Processes. If the customer explicitly named a process — "sparking", \
"wire", "grinding", "turning" — set customer_named_processes and route the job \
using those. Do NOT add processes they did not ask for in that case. Inferring \
extra operations is a much bigger leap than honouring a stated one, and an \
estimator will not thank you for quoting work nobody asked for. When the \
customer named nothing, propose the operations the drawing needs and mark them \
as your proposal.

3. Operation times. Leave set_time_mins and run_time_mins_per_unit null \
unless you are copying them from a specific past job you were given, in which \
case name that job in source_reference. A time you reasoned your way to from \
the drawing is a guess and must be null. The estimator or the calculator \
supplies real times.

4. Read the email for facts, not implications. Quantity, required date, and \
whether they mention material are facts if stated. If the email does not say, \
the field is null. Do not turn "ASAP" into a date.

5. Flag rather than resolve. If the email and the drawing disagree about \
anything — quantity above all — report it and leave it. Somebody else decides.
"""


def build_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "job_type": {
                "type": "string",
                "enum": ["service_only", "full_supply", "ambiguous"],
                "description": "Ambiguous when the signals genuinely disagree or are silent.",
            },
            "job_type_confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "job_type_reasoning": {
                "type": "string",
                "description": "One or two sentences naming the signals you used.",
            },
            "customer_named_processes": {
                "type": "array",
                "description": (
                    "Processes the customer explicitly asked for, in their own "
                    "words' terms. Empty if they named none."
                ),
                "items": {"type": "string", "enum": _ALL_PROCESSES},
            },
            "process_mix": {
                "type": "array",
                "description": (
                    "The processes this job needs. When "
                    "customer_named_processes is non-empty this must not go "
                    "beyond it."
                ),
                "items": {"type": "string", "enum": _PRODUCTION},
            },
            "proposed_operations": {
                "type": "array",
                "description": (
                    "The operation sequence, in order. Times null unless "
                    "copied from a named past job."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "op_number": {
                            "type": "integer",
                            "description": "10, 20, 30 ... in sequence.",
                        },
                        "process": {"type": "string", "enum": _ALL_PROCESSES},
                        "description": {
                            "type": "string",
                            "description": "What happens at this operation, in shop terms.",
                        },
                        "set_time_mins": {"type": ["number", "null"]},
                        "run_time_mins_per_unit": {"type": ["number", "null"]},
                        "source_reference": {
                            "type": ["string", "null"],
                            "description": (
                                "The past quote or part these times came from. "
                                "Null means you did not copy them, and then "
                                "both times must be null."
                            ),
                        },
                    },
                    "required": [
                        "op_number",
                        "process",
                        "description",
                        "set_time_mins",
                        "run_time_mins_per_unit",
                        "source_reference",
                    ],
                    "additionalProperties": False,
                },
            },
            "email_facts": {
                "type": "object",
                "description": "What the email states. Null for anything it does not.",
                "properties": {
                    "quantity": {"type": ["integer", "null"]},
                    "required_date": {
                        "type": ["string", "null"],
                        "description": "ISO date, only if the email gives a real date.",
                    },
                    "mentions_material_supply": {"type": ["boolean", "null"]},
                    "customer_reference": {
                        "type": ["string", "null"],
                        "description": "A quote or job number the email refers back to.",
                    },
                    "requests_certification": {"type": ["boolean", "null"]},
                    "urgency_wording": {
                        "type": ["string", "null"],
                        "description": "Their words about timing, quoted. Do not interpret.",
                    },
                },
                "required": [
                    "quantity",
                    "required_date",
                    "mentions_material_supply",
                    "customer_reference",
                    "requests_certification",
                    "urgency_wording",
                ],
                "additionalProperties": False,
            },
            "concerns": {
                "type": "array",
                "description": (
                    "Things an estimator should know that are not errors: "
                    "awkward features, risky tolerances, anything you would "
                    "mention if you were handing this over."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {"type": "string", "enum": ["info", "warn", "block"]},
                        "message": {"type": "string"},
                    },
                    "required": ["severity", "message"],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "job_type",
            "job_type_confidence",
            "job_type_reasoning",
            "customer_named_processes",
            "process_mix",
            "proposed_operations",
            "email_facts",
            "concerns",
        ],
        "additionalProperties": False,
    }


def build_prompt(
    *,
    part_summary: str,
    email_subject: str | None,
    email_body: str | None,
    customer_summary: str,
    history_summary: str,
) -> str:
    return "\n".join(
        [
            "--- enquiry email ---",
            f"Subject: {email_subject or '(none)'}",
            (email_body or "(no body)").strip()[:6000],
            "--- end of email ---",
            "",
            "--- read off the drawing ---",
            part_summary,
            "(Anything shown as 'not read' was either illegible or below the "
            "confidence threshold. Treat it as unknown — do not fill it in.)",
            "--- end of drawing data ---",
            "",
            "--- this customer ---",
            customer_summary,
            "--- end of customer ---",
            "",
            "--- possibly comparable past jobs ---",
            history_summary,
            "(These are candidates found by envelope, material and tolerance "
            "band. They may be irrelevant. Only copy times from one if it is "
            "genuinely the same job, and name it when you do.)",
            "--- end of past jobs ---",
            "",
            "Classify this enquiry against the schema.",
        ]
    )
