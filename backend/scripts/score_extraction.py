"""Score extraction against drawings whose answers are already known.

This is step 3 of the spec's build order, and the measurement it singles out:

    "Score AI output against what the estimator would have concluded, before
    building any UI around it. This single measurement predicts whether the
    whole project saves time or creates a new checking burden."

The headline number is not accuracy. It is **confidently wrong** — fields the
extractor was sure about and got wrong. Those are the ones that flow into a
price with nothing flagging them. A field it flagged and a human then corrected
is the system working as designed; a field it was certain about and wrong is
the failure this whole design exists to prevent.

Usage
-----
    python -m scripts.score_extraction --init ./scoring     # make the template
    # ...drop drawings in ./scoring/drawings, fill in truth.json...
    python -m scripts.score_extraction ./scoring            # run the scoring
    python -m scripts.score_extraction ./scoring --stub     # no API key needed

Needs AQM_ANTHROPIC_API_KEY unless --stub is given.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.prompts import extraction as extraction_prompt
from app.services.ai import AIError, StubAIClient, get_ai_client
from app.services.confidence import apply_policy, readings_from_payload
from app.services.rasterise import RasteriseError, rasterise_image, rasterise_pdf, to_image_blocks

#: Fields worth scoring. The rest are free text where "different" rarely means
#: "wrong", and scoring them would flatter or punish the model arbitrarily.
SCORED_FIELDS = (
    "drawing_number",
    "revision",
    "quantity",
    "material",
    "heat_treatment",
    "tightest_tolerance",
    "envelope_x",
    "envelope_y",
    "envelope_z",
)

#: Envelope dimensions within this fraction of the true value count as read
#: correctly — an estimator reading 119.98 as 120 has not made a mistake.
NUMERIC_TOLERANCE = Decimal("0.02")


class Outcome:
    CORRECT = "correct"
    CONFIDENTLY_WRONG = "confidently_wrong"
    WITHHELD_AND_WOULD_HAVE_BEEN_WRONG = "withheld_wrong"
    WITHHELD_BUT_WAS_RIGHT = "withheld_right"
    UNREAD = "unread"
    NOT_IN_TRUTH = "not_scored"


@dataclass
class FieldResult:
    drawing: str
    field_name: str
    truth: Any
    extracted: Any
    confidence: float | None
    outcome: str


@dataclass
class Report:
    results: list[FieldResult] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def scored(self) -> list[FieldResult]:
        return [r for r in self.results if r.outcome != Outcome.NOT_IN_TRUTH]

    def count(self, outcome: str) -> int:
        return sum(1 for r in self.scored() if r.outcome == outcome)


def normalise(value: Any) -> str:
    """Compare like an estimator would, not like a string comparison would."""
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = text.replace("±", "+/-").replace("–", "-").replace("—", "-")
    # "1.2312 " == "1.2312"; "H7 " == "h7"; "52-54 HRC" == "52 - 54 hrc"
    return re.sub(r"\s+", "", text)


def values_match(field_name: str, truth: Any, extracted: Any) -> bool:
    if truth is None or extracted is None:
        return truth is None and extracted is None
    if field_name.startswith("envelope") or field_name == "quantity":
        try:
            a, b = Decimal(str(truth)), Decimal(str(extracted))
        except (InvalidOperation, ValueError):
            return normalise(truth) == normalise(extracted)
        if a == 0:
            return b == 0
        return abs(a - b) / abs(a) <= NUMERIC_TOLERANCE
    return normalise(truth) == normalise(extracted)


def score_one(
    drawing: Path, truth: dict[str, Any], payload: dict[str, Any], settings
) -> list[FieldResult]:
    """Compare one extraction against the known answers."""
    field_payload = {name: payload[name] for name in extraction_prompt.FIELDS if name in payload}
    outcome = apply_policy(readings_from_payload(field_payload), settings)
    results: list[FieldResult] = []

    for name in SCORED_FIELDS:
        if name not in truth:
            continue
        expected = truth[name]
        confidence = outcome.confidences.get(name)

        if name in outcome.accepted:
            got = outcome.accepted[name]
            verdict = (
                Outcome.CORRECT if values_match(name, expected, got) else Outcome.CONFIDENTLY_WRONG
            )
        elif name in outcome.withheld:
            got = outcome.withheld[name]
            # Withholding a value that would have been right is a cost, but a
            # far smaller one than pricing a value that was wrong.
            verdict = (
                Outcome.WITHHELD_BUT_WAS_RIGHT
                if values_match(name, expected, got)
                else Outcome.WITHHELD_AND_WOULD_HAVE_BEEN_WRONG
            )
        else:
            got = None
            verdict = Outcome.UNREAD

        results.append(
            FieldResult(
                drawing=drawing.name,
                field_name=name,
                truth=expected,
                extracted=got,
                confidence=confidence,
                outcome=verdict,
            )
        )
    return results


def extract(drawing: Path, ai, settings) -> dict[str, Any]:
    data = drawing.read_bytes()
    if drawing.suffix.lower() == ".pdf":
        pages = rasterise_pdf(data)
    else:
        media = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
        pages = rasterise_image(data, media.get(drawing.suffix.lower(), "image/png"))

    return ai.structured(
        system=extraction_prompt.SYSTEM,
        prompt=extraction_prompt.build_prompt(page_count=len(pages), filename=drawing.name),
        schema=extraction_prompt.build_schema(),
        images=to_image_blocks(pages),
    )


def render(report: Report) -> None:
    scored = report.scored()
    if not scored:
        print("\nNothing was scored. Check that truth.json names the drawings.\n")
        return

    print("\n" + "=" * 78)
    print("EXTRACTION SCORING")
    print("=" * 78)

    per_field: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for result in scored:
        per_field[result.field_name][result.outcome] += 1

    print(
        f"\n{'Field':<22}{'Correct':>9}{'CONF. WRONG':>13}{'Held(wrong)':>13}{'Held(ok)':>10}{'Unread':>8}"
    )
    print("-" * 78)
    for name in SCORED_FIELDS:
        counts = per_field.get(name)
        if not counts:
            continue
        print(
            f"{name:<22}"
            f"{counts[Outcome.CORRECT]:>9}"
            f"{counts[Outcome.CONFIDENTLY_WRONG]:>13}"
            f"{counts[Outcome.WITHHELD_AND_WOULD_HAVE_BEEN_WRONG]:>13}"
            f"{counts[Outcome.WITHHELD_BUT_WAS_RIGHT]:>10}"
            f"{counts[Outcome.UNREAD]:>8}"
        )

    total = len(scored)
    wrong = report.count(Outcome.CONFIDENTLY_WRONG)
    correct = report.count(Outcome.CORRECT)
    print("-" * 78)
    print(
        f"{'TOTAL':<22}{correct:>9}{wrong:>13}"
        f"{report.count(Outcome.WITHHELD_AND_WOULD_HAVE_BEEN_WRONG):>13}"
        f"{report.count(Outcome.WITHHELD_BUT_WAS_RIGHT):>10}"
        f"{report.count(Outcome.UNREAD):>8}"
    )

    print(f"\n  Fields scored:      {total}")
    print(f"  Read and correct:   {correct} ({100 * correct / total:.0f}%)")
    print(
        f"  CONFIDENTLY WRONG:  {wrong} ({100 * wrong / total:.0f}%)   <- the number that matters"
    )

    if wrong:
        print("\n  Every one of these would have reached a price with nothing flagging it:\n")
        for result in scored:
            if result.outcome == Outcome.CONFIDENTLY_WRONG:
                score = f"{result.confidence:.2f}" if result.confidence is not None else "?"
                print(f"    {result.drawing}  {result.field_name}")
                print(f"      drawing says : {result.truth}")
                print(f"      AI read      : {result.extracted}  (confidence {score})")
    else:
        print("\n  Nothing was confidently wrong. That is the result you want.")

    held = report.count(Outcome.WITHHELD_AND_WOULD_HAVE_BEEN_WRONG)
    if held:
        print(f"\n  {held} wrong value(s) were caught by the confidence threshold and withheld.")
        print("  That is the safety net doing its job, not a failure.")

    for failure in report.failures:
        print(f"\n  FAILED: {failure}")
    print()


def init_folder(root: Path) -> int:
    drawings = root / "drawings"
    drawings.mkdir(parents=True, exist_ok=True)
    truth_path = root / "truth.json"

    if truth_path.exists():
        print(f"{truth_path} already exists; leaving it alone.")
        return 0

    template = {
        "_README": (
            "One entry per drawing file in ./drawings. Fill in ONLY what the "
            "drawing itself states — leave a field out if the drawing does not "
            "say it. Adding a value the drawing does not carry would score the "
            "AI wrong for being right."
        ),
        "4471.pdf": {
            "drawing_number": "4471",
            "revision": "B",
            "material": "1.2312",
            "quantity": 4,
            "tightest_tolerance": "+0.000/-0.013",
            "envelope_x": 120,
            "envelope_y": 80,
            "envelope_z": 25,
        },
    }
    truth_path.write_text(json.dumps(template, indent=2))
    print(
        f"\nCreated {root}/\n  drawings/   <- put the drawing files here"
        f"\n  truth.json  <- fill in what each drawing actually says\n"
    )
    print("Then run:  python -m scripts.score_extraction", root, "\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "folder", nargs="?", type=Path, help="folder holding drawings/ and truth.json"
    )
    parser.add_argument("--init", type=Path, metavar="FOLDER", help="create the folder structure")
    parser.add_argument(
        "--stub", action="store_true", help="run without an API key (plumbing check only)"
    )
    args = parser.parse_args()

    if args.init:
        return init_folder(args.init)
    if not args.folder:
        parser.error("give a folder, or --init FOLDER to create one")

    truth_path = args.folder / "truth.json"
    drawings_dir = args.folder / "drawings"
    if not truth_path.exists():
        print(f"No truth.json in {args.folder}. Run with --init first.", file=sys.stderr)
        return 1

    truths = {k: v for k, v in json.loads(truth_path.read_text()).items() if not k.startswith("_")}
    settings = get_settings()

    if args.stub:
        # Enough to prove the harness runs; the numbers mean nothing.
        ai = StubAIClient(
            [
                {
                    name: {"value": None, "confidence": None, "evidence": "stub"}
                    for name in extraction_prompt.FIELDS
                }
                for _ in truths
            ]
        )
    elif not settings.anthropic_api_key:
        print(
            "AQM_ANTHROPIC_API_KEY is not set, so extraction cannot run.\n"
            "Set it in backend/.env, or pass --stub to check the harness itself.",
            file=sys.stderr,
        )
        return 1
    else:
        ai = get_ai_client(settings)

    report = Report()
    for filename, truth in truths.items():
        drawing = drawings_dir / filename
        if not drawing.exists():
            report.failures.append(f"{filename} is in truth.json but not in drawings/")
            continue
        print(f"  reading {filename} ...", flush=True)
        try:
            payload = extract(drawing, ai, settings)
        except (AIError, RasteriseError) as exc:
            report.failures.append(f"{filename}: {exc}")
            continue
        report.results.extend(score_one(drawing, truth, payload, settings))

    render(report)
    # Non-zero when anything was confidently wrong, so CI could gate on it.
    return 1 if report.count(Outcome.CONFIDENTLY_WRONG) else 0


if __name__ == "__main__":
    raise SystemExit(main())
