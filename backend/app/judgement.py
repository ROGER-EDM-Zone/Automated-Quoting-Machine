"""The estimator's judgement, written down.

`calculators` holds what the machines do. This holds what a person who has
been quoting EDM for forty years does *with* that number — the checks they
run before believing it, and the reasons they overrule it.

It came out of a conversation between EDM Zone and Paul, their experienced
estimator, and its stated purpose is blunt: capture the judgement before it
retires. So the shape here follows *The Checklist Manifesto* rather than a
formula. A checklist does not replace expertise; it makes sure the expertise
gets applied on the day somebody is busy.

Like `pricing` and `calculators`, nothing here touches the network, the clock
or the database. Every question is data a person can read and argue with,
because the whole point is that a person disagrees with it and the system
learns from the disagreement.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


class JudgementError(Exception):
    """The inputs cannot produce advice worth acting on."""


# ---------------------------------------------------------------------------
# The checklist
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Question:
    #: Stable identifier. Answers are recorded against this, so renaming the
    #: text is safe and renaming this is not.
    key: str
    text: str
    #: A quote cannot be approved while a mandatory question is unanswered.
    #: Deliberately few: make everything compulsory and people tick blindly.
    mandatory: bool = False
    #: Shown alongside the question — the reason it is on the list at all.
    why: str | None = None


@dataclass(frozen=True)
class Section:
    number: int
    title: str
    questions: tuple[Question, ...]


#: The eight stages, in the order they are asked. The order is not cosmetic:
#: workholding and distortion come *before* the time calculation, because
#: both change the time, and a checklist that asks them afterwards is only
#: recording regret.
CHECKLIST: tuple[Section, ...] = (
    Section(1, "Basic job information", (
        Question("customer_ref", "Customer / enquiry reference"),
        Question("drawing_rev", "Drawing number and revision", mandatory=True,
                 why="A price against the wrong issue is worse than no price."),
        Question("quantity", "Quantity", mandatory=True),
        Question("process", "Wire EDM / spark EDM / both", mandatory=True),
        Question("material_supply", "Free issue, or EDM Zone supplying material?",
                 mandatory=True,
                 why="Changes both the price and who carries the scrap risk."),
        Question("repeat_job", "Repeat job? If yes, previous price checked?",
                 why="The best guide to the right price is what was charged last time."),
    )),
    Section(2, "Material", (
        Question("material_identified", "Material identified", mandatory=True),
        Question("material_difficulty", "Normal, or difficult and slower to cut?"),
        Question("cutting_allowance", "Cutting characteristics allowed for"),
        Question("heat_treatment", "Heat treatment or hardness relevant?"),
    )),
    Section(3, "Geometry and machine capability", (
        Question("cut_height", "Maximum cutting height / thickness", mandatory=True,
                 why="Height drives wire speed harder than anything else."),
        Question("cut_length", "Total cutting length", mandatory=True),
        Question("taper", "Taper or angle required"),
        Question("wire_dia", "Wire diameter required"),
        Question("starter_hole", "Starter hole available?"),
        Question("fits_travels", "Component fits the machine travels?", mandatory=True),
        Question("within_weight", "Component within the machine weight limit?", mandatory=True),
        Question("machine_choice", "Best EDM machine selected?"),
    )),
    Section(4, "Tolerance and finish", (
        Question("dimensional_tol", "Dimensional tolerance checked", mandatory=True),
        Question("geometric_tol", "Geometric tolerance checked"),
        Question("surface_finish", "Surface finish checked"),
        Question("skim_cuts", "Number of skim cuts determined", mandatory=True,
                 why="Every skim travels the whole path again."),
        Question("inspection_method", "Inspection method decided"),
    )),
    Section(5, "Workholding", (
        Question("how_held", "How are we actually going to hold this?", mandatory=True,
                 why="The question an inexperienced estimator skips while "
                     "concentrating on the drawing."),
        Question("clamping_area", "Enough clamping area?"),
        Question("special_fixture", "Special fixture required?"),
        Question("set_square", "Can it be set square easily?"),
        Question("access_flushing", "Does the setup restrict wire access or flushing?"),
    )),
    Section(6, "Movement and distortion", (
        Question("stress_release", "Will the material move when the first cut releases stress?",
                 mandatory=True),
        Question("thin_wall", "Thin wall remaining?"),
        Question("section_springs", "Section likely to spring open or closed?"),
        Question("sequence_change", "Does the machining sequence need changing?"),
        Question("bridges_tags", "Bridges or tags required?"),
    )),
    Section(7, "Time calculation", (
        Question("programming", "Programming"),
        Question("setup", "Setup"),
        Question("cutting", "Cutting time"),
        Question("skims", "Skim cuts"),
        Question("handling", "Handling between components"),
        Question("inspection", "Inspection"),
        Question("electrodes", "Electrode manufacture / sparking"),
        Question("contingency", "Contingency and risk"),
    )),
    Section(8, "Commercial sense-check", (
        Question("one_off_or_production", "One-off or production quantity?", mandatory=True),
        Question("handling_per_component", "Handling and setup per component?"),
        Question("free_issue_risk", "Risk of scrapping expensive free-issue material?",
                 mandatory=True,
                 why="Scrapping a customer's part costs more than the operation."),
        Question("specialist_value", "Specialist capability, or value to the customer?"),
        Question("repeat_potential", "Repeat potential?"),
        Question("historical_price", "What have we historically charged for comparable work?",
                 mandatory=True),
        Question("min_order", "Minimum order value considered?"),
        Question("feels_right", "Does this price actually feel commercially right?",
                 mandatory=True,
                 why="The last gate, and the one the arithmetic cannot answer."),
    )),
)

#: Every mandatory question, flattened. A quote cannot leave review while any
#: of these is unanswered.
MANDATORY_KEYS: frozenset[str] = frozenset(
    q.key for section in CHECKLIST for q in section.questions if q.mandatory
)


def unanswered_mandatory(answers: dict[str, object]) -> tuple[Question, ...]:
    """Mandatory questions with nothing recorded against them.

    An explicit "not applicable" counts as answered; a blank or a missing key
    does not. That distinction is the whole mechanism — the checklist works
    by making somebody say N/A out loud rather than skipping past.
    """
    out = []
    for section in CHECKLIST:
        for q in section.questions:
            if not q.mandatory:
                continue
            value = answers.get(q.key)
            if value is None or (isinstance(value, str) and not value.strip()):
                out.append(q)
    return tuple(out)


# ---------------------------------------------------------------------------
# Materials that do not cut like steel
# ---------------------------------------------------------------------------

#: Materials whose EDM behaviour differs enough from steel that a time built
#: on the steel speed table is wrong. The multiplier is applied to cutting
#: time, not to the price.
#:
#: These are the shop's own warnings, not published figures, and the
#: multipliers are first estimates awaiting real numbers — which is why
#: `warning` carries the words and the number is kept conservative.
MATERIAL_DIFFICULTY: dict[str, tuple[Decimal, str]] = {
    "phosphor bronze": (
        Decimal("1.40"),
        "PHOSPHOR BRONZE — allow for slower cutting than standard steel.",
    ),
    "titanium": (
        Decimal("1.35"),
        "TITANIUM — cuts slower than steel and is prone to wire breakage.",
    ),
    "tungsten": (
        Decimal("1.60"),
        "TUNGSTEN ALLOY — much slower than steel. Do not price off the steel table.",
    ),
    "copper": (
        Decimal("1.25"),
        "COPPER — high conductivity; expect slower cutting than steel.",
    ),
    "carbide": (
        Decimal("1.50"),
        "CARBIDE — slow, and cobalt leaching risks the surface. Allow extra skims.",
    ),
}


def material_warning(spec: str | None) -> tuple[Decimal, str] | None:
    """Difficulty multiplier and warning for a material, or None if it cuts
    like steel. Matched on a substring so "Phosphor Bronze PB1" is caught."""
    if not spec:
        return None
    lowered = spec.lower()
    for needle, result in MATERIAL_DIFFICULTY.items():
        if needle in lowered:
            return result
    return None


# ---------------------------------------------------------------------------
# The long-runner adjustment
# ---------------------------------------------------------------------------

#: Cutting hours at or above which a reduced running rate becomes arguable.
LONG_RUNNER_THRESHOLD_HOURS = Decimal("10")

#: How far below the standard rate the ladder goes, and in what steps. Both
#: come from the estimator working in £2.50 steps: at a £60 standard rate the
#: ladder he quoted is £50.00 / £52.50 / £55.00 / £57.50 / £60.00.
LADDER_STEP = Decimal("2.50")
LADDER_STEPS_BELOW = 4

#: Every one must be true before a reduced rate is defensible. The reasoning
#: matters more than the threshold: expensive operator and setup time and
#: relatively "free" machine-running time do not deserve the same rate.
LONG_RUNNER_GATES: tuple[Question, ...] = (
    Question("simple_setup", "Is the setup straightforward?"),
    Question("runs_unattended", "Can the machine run unattended?"),
    Question("low_intervention", "Is there little operator intervention?"),
    Question("large_batch", "Is it a large batch?"),
    Question("low_scrap_risk", "Is there low scrap and low risk?"),
    Question("out_of_hours", "Can it run overnight or at weekends?"),
)


@dataclass(frozen=True)
class LadderRung:
    hourly_rate: Decimal
    value: Decimal
    #: True for the shop's standard rate — the rung nothing was discounted from.
    is_standard: bool


def _q2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def rate_ladder(standard_rate: Decimal, cutting_hours: Decimal) -> tuple[LadderRung, ...]:
    """Selling price at each rung, cheapest first.

    Deliberately returns all five rather than picking one. The estimator asked
    for exactly that: see the price at every rate, then choose, and be able to
    say why. A function that chose for him would throw away the judgement this
    module exists to keep.
    """
    if standard_rate <= 0:
        raise JudgementError("An hourly rate must be greater than zero")
    if cutting_hours < 0:
        raise JudgementError("Cutting hours cannot be negative")

    rungs = []
    for step in range(LADDER_STEPS_BELOW, -1, -1):
        rate = standard_rate - LADDER_STEP * step
        if rate <= 0:
            continue
        rungs.append(
            LadderRung(
                hourly_rate=_q2(rate),
                value=_q2(rate * cutting_hours),
                is_standard=(step == 0),
            )
        )
    return tuple(rungs)


def is_long_runner(cutting_hours: Decimal) -> bool:
    """Whether the job is long enough for the reduced-rate question to arise."""
    return cutting_hours >= LONG_RUNNER_THRESHOLD_HOURS


# ---------------------------------------------------------------------------
# Benchmarks — prices the business actually charged
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Benchmark:
    description: str
    quantity: int
    price_each: Decimal
    note: str


#: Jobs the shop has sold, kept as a sanity check on the arithmetic. One entry
#: is not a database; it is the start of one, and it is here because the
#: estimator raised it unprompted as the kind of price the calculator would
#: not have arrived at on its own.
BENCHMARKS: tuple[Benchmark, ...] = (
    Benchmark(
        description="COMET keyways",
        quantity=50,
        price_each=Decimal("16.00"),
        note="Raised as an example of an experienced price differing from a "
             "machine-time calculation. The reasoning behind it has not been "
             "recorded yet and needs to be.",
    ),
)
