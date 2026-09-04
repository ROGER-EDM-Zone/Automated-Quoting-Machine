"""Deterministic material nesting (spec stage 4).

"Material nesting (blanks per sheet, utilisation) is deterministic too: part
envelope + quantity + standard stock sizes you actually buy. Not an AI
judgement."

Like `pricing`, this module is pure arithmetic over plain data. It answers
"how many blanks come out of one piece of stock, how many pieces do we buy,
and what does that cost" — nothing else.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.enums import StockForm

ZERO = Decimal("0.00")


def _q2(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class NestingError(Exception):
    """The part cannot be nested from the stock offered."""


@dataclass(frozen=True)
class StockOption:
    """One row of `stock_size` — stock the shop actually buys."""

    stock_id: int
    spec: str
    stock_form: str
    length_mm: Decimal
    width_mm: Decimal | None
    thickness_mm: Decimal | None
    unit_cost: Decimal
    kerf_mm: Decimal = Decimal("3")

    def label(self) -> str:
        dims = [d for d in (self.length_mm, self.width_mm, self.thickness_mm) if d]
        return f"{self.spec} {' x '.join(str(_q2(d)) for d in dims)}mm"


@dataclass(frozen=True)
class PartEnvelope:
    x_mm: Decimal
    y_mm: Decimal
    z_mm: Decimal
    #: True when the part is turned — round about one axis rather than a
    #: block. It changes the bar size materially: a 85mm disc modelled as a
    #: 85 x 85 block needs the *diagonal* of that square to clear the bore,
    #: so 121mm of bar, when 85mm of bar is plainly enough. Set from the
    #: drawing and the routing, never assumed.
    is_rotational: bool = False

    def sorted_dims(self) -> tuple[Decimal, Decimal, Decimal]:
        dims = sorted((Decimal(self.x_mm), Decimal(self.y_mm), Decimal(self.z_mm)))
        return dims[2], dims[1], dims[0]  # largest first


@dataclass(frozen=True)
class Allowance:
    """Material left on for machining, before any stock size is considered.

    Without this the nester fits the finished part into the bar and concludes
    a 85mm part comes out of 85mm stock — which is not a part, it is a bar
    with nothing to clean up. The shop's own rule of thumb ("3-5mm on the OD,
    3-5mm on the length") is what this carries, and it comes from the rules
    table rather than from here: there is deliberately no default, because a
    guessed allowance is a guessed price.
    """

    #: Added to the diameter for round stock, and to each section dimension
    #: for square stock and plate.
    section_mm: Decimal = Decimal("0")
    #: Added to the dimension running along the bar, before the parting kerf.
    length_mm: Decimal = Decimal("0")

    @property
    def is_zero(self) -> bool:
        return Decimal(self.section_mm) == 0 and Decimal(self.length_mm) == 0


NO_ALLOWANCE = Allowance()


@dataclass(frozen=True)
class NestingResult:
    stock_id: int
    stock_label: str
    stock_form: str
    stock_size: str
    blanks_per_unit_stock: int
    #: Pieces of stock to buy for the whole quantity.
    qty_required: Decimal
    unit_cost: Decimal
    total_cost: Decimal
    #: Part volume consumed / stock volume bought, as a percentage.
    utilisation_pct: Decimal
    #: The size the part actually needs once the machining allowance is on —
    #: the "I asked for 92mm" half of the answer. Reported so the workspace
    #: can show why a 100mm bar was chosen rather than presenting the 100 as
    #: though it were the requirement.
    required_section_mm: Decimal | None = None
    required_length_mm: Decimal | None = None
    #: How much bigger the chosen stock is than the requirement, across the
    #: section. Large numbers here are the ones worth a second look.
    section_oversize_mm: Decimal | None = None
    allowance_applied: bool = False
    #: Every stock form that was actually available to choose from. A cube
    #: quoted out of round bar is not wrong arithmetic, it is a missing
    #: option — and the only way to tell the two apart is to know what was on
    #: offer when the choice was made.
    forms_offered: tuple[str, ...] = ()


@dataclass(frozen=True)
class Fit:
    """How one part sits in one stock size, once the allowance is on."""

    blanks: int
    #: Section the part needs — the bar diameter, the square across flats, or
    #: the plate thickness, depending on the form.
    required_section_mm: Decimal
    #: Length of stock one blank consumes, allowance included, kerf excluded.
    required_length_mm: Decimal


def _grow(value: Decimal, by: Decimal) -> Decimal:
    return Decimal(value) + Decimal(by)


def _orientations(
    part: PartEnvelope,
) -> list[tuple[Decimal, Decimal, Decimal]]:
    """Every way round the part can sit in a bar: (axial, section, section).

    Only the largest dimension used to be tried, which quietly ruled out the
    cheapest answer for anything disc-shaped — a 85 x 85 x 20 part wants its
    20 running along the bar, not its 85.
    """
    x, y, z = Decimal(part.x_mm), Decimal(part.y_mm), Decimal(part.z_mm)
    return [(x, y, z), (y, x, z), (z, x, y)]


def fit_in_stock(
    part: PartEnvelope,
    stock: StockOption,
    allowance: Allowance = NO_ALLOWANCE,
) -> Fit:
    """Fit one part into one stock size. ``blanks`` is 0 when it will not go.

    The allowance is added to the part *before* anything is compared, so the
    size that gets looked for is the size that has to be bought, not the size
    that gets shipped.
    """
    kerf = Decimal(stock.kerf_mm)
    px, py, pz = Decimal(part.x_mm), Decimal(part.y_mm), Decimal(part.z_mm)
    if min(px, py, pz) <= 0:
        raise NestingError("Part envelope must be positive in all three axes")

    section_add = Decimal(allowance.section_mm)
    length_add = Decimal(allowance.length_mm)
    form = stock.stock_form

    if form == StockForm.PLATE.value:
        if stock.width_mm is None or stock.thickness_mm is None:
            raise NestingError(f"Plate stock {stock.stock_id} needs width and thickness")
        big, mid, small = part.sorted_dims()
        # Thickness has to cover the part plus clean-up on both faces.
        need_thickness = _grow(small, section_add)
        need_a, need_b = _grow(big, length_add), _grow(mid, length_add)
        if need_thickness > Decimal(stock.thickness_mm):
            return Fit(0, need_thickness, need_a)
        best = 0
        for a, b in ((need_a, need_b), (need_b, need_a)):
            along = int(Decimal(stock.length_mm) // (a + kerf))
            across = int(Decimal(stock.width_mm) // (b + kerf))
            best = max(best, along * across)
        return Fit(best, need_thickness, need_a)

    if form in (StockForm.BAR_SQUARE.value, StockForm.BILLET.value):
        if stock.width_mm is None or stock.thickness_mm is None:
            raise NestingError(f"Bar stock {stock.stock_id} needs section dimensions")
        section = sorted((Decimal(stock.width_mm), Decimal(stock.thickness_mm)))
        best: Fit | None = None
        for axial, s1, s2 in _orientations(part):
            small_side, large_side = sorted((_grow(s1, section_add), _grow(s2, section_add)))
            need_length = _grow(axial, length_add)
            blanks = (
                0
                if large_side > section[1] or small_side > section[0]
                else max(0, int(Decimal(stock.length_mm) // (need_length + kerf)))
            )
            candidate = Fit(blanks, large_side, need_length)
            if best is None or _better(candidate, best):
                best = candidate
        assert best is not None
        return best

    if form in (StockForm.BAR_ROUND.value, StockForm.TUBE.value):
        diameter = Decimal(stock.width_mm or 0)
        if diameter <= 0:
            raise NestingError(f"Round stock {stock.stock_id} needs a diameter")
        best_round: Fit | None = None
        for axial, s1, s2 in _orientations(part):
            # A turned part is already round, so its own diameter is what has
            # to clear — the larger of the two section dimensions. A block has
            # to have its corners cleared, so the diagonal governs. Getting
            # this wrong buys 40% more steel than the job needs.
            if part.is_rotational:
                section = max(Decimal(s1), Decimal(s2))
            else:
                section = (Decimal(s1) * Decimal(s1) + Decimal(s2) * Decimal(s2)).sqrt()
            need_diameter = _grow(section, section_add)
            need_length = _grow(axial, length_add)
            blanks = (
                0
                if need_diameter > diameter
                else max(0, int(Decimal(stock.length_mm) // (need_length + kerf)))
            )
            candidate = Fit(blanks, need_diameter, need_length)
            if best_round is None or _better(candidate, best_round):
                best_round = candidate
        assert best_round is not None
        return best_round

    raise NestingError(f"Unknown stock form '{form}'")


def _better(candidate: Fit, incumbent: Fit) -> bool:
    """More blanks wins; on a tie, the orientation needing less stock wins."""
    if candidate.blanks != incumbent.blanks:
        return candidate.blanks > incumbent.blanks
    return candidate.required_section_mm < incumbent.required_section_mm


def blanks_per_stock(
    part: PartEnvelope,
    stock: StockOption,
    allowance: Allowance = NO_ALLOWANCE,
) -> int:
    """How many blanks fit in one piece of stock. 0 if the part will not fit."""
    return fit_in_stock(part, stock, allowance).blanks


def _stock_volume(stock: StockOption) -> Decimal:
    length = Decimal(stock.length_mm)
    if stock.stock_form in (StockForm.BAR_ROUND.value, StockForm.TUBE.value):
        radius = Decimal(stock.width_mm or 0) / 2
        return Decimal(str(math.pi)) * radius * radius * length
    return length * Decimal(stock.width_mm or 0) * Decimal(stock.thickness_mm or 0)


def _stock_section(stock: StockOption) -> Decimal | None:
    """The dimension the required section is compared against, for reporting."""
    if stock.stock_form in (StockForm.BAR_ROUND.value, StockForm.TUBE.value):
        return Decimal(stock.width_mm) if stock.width_mm is not None else None
    if stock.stock_form == StockForm.PLATE.value:
        return Decimal(stock.thickness_mm) if stock.thickness_mm is not None else None
    if stock.width_mm is None or stock.thickness_mm is None:
        return None
    return max(Decimal(stock.width_mm), Decimal(stock.thickness_mm))


def nest(
    part: PartEnvelope,
    quantity: int,
    options: Sequence[StockOption],
    allowance: Allowance = NO_ALLOWANCE,
) -> NestingResult:
    """Pick the cheapest stock that yields ``quantity`` blanks.

    Two steps, in this order: grow the part by the machining allowance to get
    the size that actually has to be bought, then take the cheapest stock the
    supplier holds that is at least that size. That is why the answer to "I
    need 92mm" is "buy the 100mm bar" rather than "buy 92mm" — nobody sells
    92mm, and pricing 92mm of steel prices a bar that will never arrive.

    Deterministic: candidates are ranked by total cost, then by fewer pieces of
    stock, then by higher utilisation, then by ``stock_id``. No AI, no
    tie-breaking by iteration order.
    """
    if quantity < 1:
        raise NestingError(f"Quantity must be at least 1, got {quantity}")
    if not options:
        raise NestingError("No stock sizes available for this specification")

    part_volume = Decimal(part.x_mm) * Decimal(part.y_mm) * Decimal(part.z_mm)
    candidates: list[tuple[tuple, NestingResult]] = []
    forms_offered = tuple(sorted({stock.stock_form for stock in options}))

    for stock in options:
        fit = fit_in_stock(part, stock, allowance)
        if fit.blanks < 1:
            continue
        pieces = math.ceil(quantity / fit.blanks)
        total_cost = _q2(Decimal(pieces) * Decimal(stock.unit_cost))
        bought_volume = _stock_volume(stock) * Decimal(pieces)
        utilisation = (
            _q2((part_volume * Decimal(quantity) / bought_volume) * Decimal("100"))
            if bought_volume > 0
            else ZERO
        )
        held = _stock_section(stock)
        result = NestingResult(
            stock_id=stock.stock_id,
            stock_label=stock.label(),
            stock_form=stock.stock_form,
            stock_size=stock.label(),
            blanks_per_unit_stock=fit.blanks,
            qty_required=Decimal(pieces),
            unit_cost=_q2(stock.unit_cost),
            total_cost=total_cost,
            utilisation_pct=utilisation,
            required_section_mm=_q2(fit.required_section_mm),
            required_length_mm=_q2(fit.required_length_mm),
            section_oversize_mm=(_q2(held - fit.required_section_mm) if held is not None else None),
            allowance_applied=not allowance.is_zero,
            forms_offered=forms_offered,
        )
        candidates.append(((total_cost, pieces, -utilisation, stock.stock_id), result))

    if not candidates:
        raise NestingError("Part does not fit any available stock size — needs a buyer decision")
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]
