"""Reading a supplier's page for today's price and today's size range.

The rule this prompt exists to enforce: the model reports what the fetched
page says, and nothing else. It does not recall a price, it does not average
what it knows about the market, and it does not convert a figure it is unsure
of. Every value comes back with the sentence it was read from, and a value
with no supporting text is treated as unread — the same standard a dimension
on a drawing is held to.

That is what makes this "live data" rather than "a plausible number": there is
always a page, a quoted line on it, and a timestamp.
"""

from __future__ import annotations

from app.enums import MarketUnit

#: The unit values may come back in. The model picks one; it never invents a
#: unit, and it never silently converts between them.
UNITS = tuple(u.value for u in MarketUnit)

SYSTEM = """You read supplier and market pages and report exactly what they say.

You are supporting a subcontract machine shop's quoting system. A number you
report will be multiplied by a weight and sent to a customer as a price, so a
wrong one costs real money and a missing one costs nothing but a phone call.

Rules, in order of importance:

1. Report only what appears in the page text you are given. If the page does
   not state a price, say so by returning null. Never supply a price from your
   own knowledge of the market, however confident you are — a remembered price
   is not live data, and this system's whole purpose is to know the difference.
2. Quote the exact text you read each value from, verbatim, in `evidence`.
   A value without supporting text in the page will be discarded.
3. Give the unit exactly as the page states it. Do not convert. If the page
   prices per metre and the caller wanted per kilogram, report per metre and
   let the caller decide.
4. State whether the price shown includes VAT, when the page says. UK trade
   pages usually quote ex-VAT and consumer pages usually do not, but only
   report what is written.
5. `confidence` is your honest read of whether you have the right number for
   what was asked — not how clearly the text was formatted. A page showing six
   prices where you had to choose one is low confidence even if every figure
   was legible.
6. For a stock size range, list every size the page offers, in millimetres,
   as numbers. An incomplete list is worse than an empty one: if the page
   paginates or hides sizes behind a control, return what you can see and say
   so in `notes`.
"""


def build_prompt(*, source_name: str, target: str | None, url: str, text: str) -> str:
    wanted = target or "the current price, and the range of stock sizes offered"
    return (
        f"Source: {source_name}\n"
        f"URL: {url}\n\n"
        f"What this source is being consulted for:\n{wanted}\n\n"
        "Below is the text of that page as fetched just now. Read it and "
        "report what it says. If it does not say, return null rather than "
        "your own estimate.\n\n"
        "--- BEGIN PAGE TEXT ---\n"
        f"{text}\n"
        "--- END PAGE TEXT ---\n"
    )


def build_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["value", "unit", "confidence", "evidence", "sizes_mm", "notes"],
        "properties": {
            "value": {
                "type": ["number", "null"],
                "description": (
                    "The price the page states, in the unit given. Null when "
                    "the page does not state one."
                ),
            },
            "unit": {
                "type": ["string", "null"],
                "enum": [*UNITS, None],
                "description": "The unit the page states. Never converted.",
            },
            "includes_vat": {
                "type": ["boolean", "null"],
                "description": "Only when the page says. Null when it does not.",
            },
            "confidence": {
                "type": ["number", "null"],
                "minimum": 0,
                "maximum": 1,
                "description": "How sure you are this is the number that was asked for.",
            },
            "evidence": {
                "type": ["string", "null"],
                "description": "The exact text the value was read from, verbatim.",
            },
            "sizes_mm": {
                "type": ["array", "null"],
                "items": {"type": "number"},
                "description": (
                    "Every stock size the page offers, in millimetres. Null "
                    "when the page is not a size range."
                ),
            },
            "as_of": {
                "type": ["string", "null"],
                "description": "Any date the page gives for the price, as written.",
            },
            "notes": {
                "type": ["string", "null"],
                "description": (
                    "Anything a buyer would want flagged: a minimum order, a "
                    "quantity break, sizes hidden behind a control, a price "
                    "shown only on request."
                ),
            },
        },
    }
