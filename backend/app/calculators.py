"""Shop-floor calculators. Pure arithmetic over the shop's own figures.

Like `pricing` and `nesting`, nothing here touches the network, the clock or
the database. It exists so that a wire time is *calculated* rather than
estimated — the difference the spec draws between `time_source=calculator`
and `time_source=historical_estimate`, and the difference between a number an
estimator can trust blind and one they have to check.

The speeds are EDM Zone's own, lifted from the costing spreadsheet the shop
has used for years. They are not published figures and not a manufacturer's
claim: they are what these machines, on this shop floor, actually achieve.
That is what makes them worth encoding — and it is also why they belong in a
table a person can read and argue with, rather than buried inside a formula.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

#: Wire cutting speed in mm/min, by job height in mm — the roughing pass.
#: Speed falls away sharply with height: a 100mm job cuts at an eighth of the
#: rate of a 10mm one, which is why height matters more than path length on
#: tall work.
FIRST_CUT_SPEED: dict[int, Decimal] = {
    5: Decimal("8.8"),
    10: Decimal("8"),
    15: Decimal("6.9"),
    20: Decimal("5.8"),
    25: Decimal("5.2"),
    30: Decimal("4.5"),
    35: Decimal("4"),
    40: Decimal("3.5"),
    45: Decimal("3.1"),
    50: Decimal("2.7"),
    55: Decimal("2.5"),
    60: Decimal("2.2"),
    65: Decimal("2"),
    70: Decimal("1.8"),
    75: Decimal("1.6"),
    80: Decimal("1.5"),
    85: Decimal("1.4"),
    90: Decimal("1.3"),
    95: Decimal("1.2"),
    100: Decimal("1"),
    125: Decimal("0.85"),
    150: Decimal("0.63"),
    175: Decimal("0.49"),
    200: Decimal("0.41"),
    250: Decimal("0.29"),
    300: Decimal("0.21"),
    350: Decimal("0.16"),
    400: Decimal("0.13"),
}

#: Skim passes, far faster because they remove almost nothing.
#:
#: Note 150mm: the shop's sheet reads 2.7, sitting above both its neighbours
#: (125 -> 2.5, 175 -> 2.5) where every other entry falls as height rises.
#: It is almost certainly a typo. It is reproduced exactly as the shop has
#: it, because silently "correcting" a figure the business has quoted from
#: for years would change prices without anybody deciding to.
#: `SUSPECT_FINISH_HEIGHTS` names it so a quote built on it can say so.
FINISH_CUT_SPEED: dict[int, Decimal] = {
    5: Decimal("9.5"),
    10: Decimal("8.1"),
    15: Decimal("7"),
    20: Decimal("6"),
    25: Decimal("5.5"),
    30: Decimal("4.9"),
    35: Decimal("4.7"),
    40: Decimal("4.4"),
    45: Decimal("4.2"),
    50: Decimal("4"),
    55: Decimal("3.9"),
    60: Decimal("3.7"),
    65: Decimal("3.6"),
    70: Decimal("3.4"),
    75: Decimal("3.3"),
    80: Decimal("3.2"),
    85: Decimal("3.1"),
    90: Decimal("3"),
    95: Decimal("2.9"),
    100: Decimal("2.8"),
    125: Decimal("2.5"),
    150: Decimal("2.7"),
    175: Decimal("2.5"),
    200: Decimal("2.4"),
    250: Decimal("2.1"),
    300: Decimal("1.8"),
    350: Decimal("1.5"),
    400: Decimal("1.2"),
}

#: Heights where the shop's own table is not monotonic, so a time built on it
#: deserves a second look rather than silent trust.
SUSPECT_FINISH_HEIGHTS: frozenset[int] = frozenset({150})

#: Minutes to thread the wire and pick up the start point, per thread. A part
#: with four internal windows needs four threads, plus one for the outside.
MINUTES_PER_THREAD = Decimal("3")


class CalculatorError(Exception):
    """The inputs cannot produce a time worth quoting."""


@dataclass(frozen=True)
class WireCut:
    """One wire job, described the way an estimator describes it."""

    #: Height of material the wire cuts through, mm.
    job_height_mm: Decimal
    #: Total path the wire travels, mm — the perimeter of everything being
    #: cut, not the size of the part.
    cut_length_mm: Decimal
    #: One roughing pass, plus this many skims. Two is a normal finish; a fit
    #: or a fine surface may want three or four.
    finish_cuts: int = 1
    #: Separate threads needed. Every internal shape needs its own.
    threads: int = 1


@dataclass(frozen=True)
class WireCutResult:
    first_cut_mins: Decimal
    finish_cut_mins: Decimal
    threading_mins: Decimal
    total_mins: Decimal
    #: The speeds used, so the workspace can show the arithmetic and not just
    #: its answer.
    first_cut_speed: Decimal
    finish_cut_speed: Decimal
    #: A speed came from the questionable row of the shop's table.
    speed_is_suspect: bool
    #: The height fell between two rows, so the speed was interpolated.
    height_interpolated: bool


def _q2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def speed_at(height_mm: Decimal, table: dict[int, Decimal]) -> tuple[Decimal, bool]:
    """Cut speed for a height, interpolating between the rows we have.

    The shop's table steps — 100mm, then 125mm — and real parts do not.
    Straight-line interpolation between the two nearest rows is honest about
    that: it never invents a speed outside the measured range, and it reports
    when it interpolated so nobody mistakes the result for a measured figure.
    """
    if height_mm <= 0:
        raise CalculatorError("Job height must be greater than zero")

    heights = sorted(table)
    lowest, highest = heights[0], heights[-1]

    if height_mm < lowest:
        # Below the smallest measured height the machine does not keep
        # getting faster; hold the fastest speed actually recorded.
        return table[lowest], True
    if height_mm > highest:
        raise CalculatorError(
            f"A job {height_mm}mm tall is outside the speeds this shop has "
            f"recorded (up to {highest}mm). It needs timing by hand."
        )

    exact = int(height_mm)
    if Decimal(exact) == height_mm and exact in table:
        return table[exact], False

    index = bisect_left(heights, float(height_mm))
    below, above = heights[index - 1], heights[index]
    position = (height_mm - Decimal(below)) / Decimal(above - below)
    return table[below] + (table[above] - table[below]) * position, True


def wire_cut_time(cut: WireCut) -> WireCutResult:
    """Minutes for a wire job, from the shop's own cutting speeds.

    Roughing pass, plus skims, plus threading. Deliberately excludes setting
    the job on the machine: that is the shop's standing set time and belongs
    in the operation's set minutes, not buried inside a cutting formula.
    """
    if cut.cut_length_mm <= 0:
        raise CalculatorError("Cut length must be greater than zero")
    if cut.finish_cuts < 0:
        raise CalculatorError("Finish cuts cannot be negative")
    if cut.threads < 1:
        raise CalculatorError("A wire job needs at least one thread")

    first_speed, first_interpolated = speed_at(cut.job_height_mm, FIRST_CUT_SPEED)
    finish_speed, finish_interpolated = speed_at(cut.job_height_mm, FINISH_CUT_SPEED)

    first_mins = cut.cut_length_mm / first_speed
    # Every skim travels the whole path again.
    finish_mins = (cut.cut_length_mm / finish_speed) * Decimal(cut.finish_cuts)
    threading_mins = MINUTES_PER_THREAD * Decimal(cut.threads)

    nearest = min(FINISH_CUT_SPEED, key=lambda h: abs(Decimal(h) - cut.job_height_mm))

    return WireCutResult(
        first_cut_mins=_q2(first_mins),
        finish_cut_mins=_q2(finish_mins),
        threading_mins=_q2(threading_mins),
        total_mins=_q2(first_mins + finish_mins + threading_mins),
        first_cut_speed=_q2(first_speed),
        finish_cut_speed=_q2(finish_speed),
        speed_is_suspect=nearest in SUSPECT_FINISH_HEIGHTS,
        height_interpolated=first_interpolated or finish_interpolated,
    )
