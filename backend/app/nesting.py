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
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Sequence

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

    def sorted_dims(self) -> tuple[Decimal, Decimal, Decimal]:
        dims = sorted((Decimal(self.x_mm), Decimal(self.y_mm), Decimal(self.z_mm)))
        return dims[2], dims[1], dims[0]  # largest first


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


def blanks_per_stock(part: PartEnvelope, stock: StockOption) -> int:
    """How many blanks fit in one piece of stock. 0 if the part will not fit.

    Both plate orientations are tried and the better one wins; the kerf/grip
    allowance is added around every blank in every cutting direction.
    """
    kerf = Decimal(stock.kerf_mm)
    px, py, pz = Decimal(part.x_mm), Decimal(part.y_mm), Decimal(part.z_mm)
    if min(px, py, pz) <= 0:
        raise NestingError("Part envelope must be positive in all three axes")

    form = stock.stock_form

    if form == StockForm.PLATE.value:
        if stock.width_mm is None or stock.thickness_mm is None:
            raise NestingError(f"Plate stock {stock.stock_id} needs width and thickness")
        # The part's smallest dimension must come out of the plate thickness.
        big, mid, small = part.sorted_dims()
        if small > Decimal(stock.thickness_mm):
            return 0
        best = 0
        for a, b in ((big, mid), (mid, big)):
            along = int((Decimal(stock.length_mm)) // (a + kerf))
            across = int((Decimal(stock.width_mm)) // (b + kerf))
            best = max(best, along * across)
        return best

    if form in (StockForm.BAR_SQUARE.value, StockForm.BILLET.value):
        if stock.width_mm is None or stock.thickness_mm is None:
            raise NestingError(f"Bar stock {stock.stock_id} needs section dimensions")
        big, mid, small = part.sorted_dims()
        section = sorted((Decimal(stock.width_mm), Decimal(stock.thickness_mm)))
        # Cut along the bar length: the two smaller part dims sit in section.
        if mid > section[1] or small > section[0]:
            return 0
        return max(0, int(Decimal(stock.length_mm) // (big + kerf)))

    if form in (StockForm.BAR_ROUND.value, StockForm.TUBE.value):
        diameter = Decimal(stock.width_mm or 0)
        if diameter <= 0:
            raise NestingError(f"Round stock {stock.stock_id} needs a diameter")
        big, mid, small = part.sorted_dims()
        # The part's two smaller dims must fit inside the circle: their
        # diagonal has to clear the diameter.
        diagonal = (mid * mid + small * small).sqrt()
        if diagonal > diameter:
            return 0
        return max(0, int(Decimal(stock.length_mm) // (big + kerf)))

    raise NestingError(f"Unknown stock form '{form}'")


def _stock_volume(stock: StockOption) -> Decimal:
    length = Decimal(stock.length_mm)
    if stock.stock_form in (StockForm.BAR_ROUND.value, StockForm.TUBE.value):
        radius = Decimal(stock.width_mm or 0) / 2
        return Decimal(str(math.pi)) * radius * radius * length
    return length * Decimal(stock.width_mm or 0) * Decimal(stock.thickness_mm or 0)


def nest(
    part: PartEnvelope,
    quantity: int,
    options: Sequence[StockOption],
) -> NestingResult:
    """Pick the cheapest stock that yields ``quantity`` blanks.

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

    for stock in options:
        per_stock = blanks_per_stock(part, stock)
        if per_stock < 1:
            continue
        pieces = math.ceil(quantity / per_stock)
        total_cost = _q2(Decimal(pieces) * Decimal(stock.unit_cost))
        bought_volume = _stock_volume(stock) * Decimal(pieces)
        utilisation = (
            _q2((part_volume * Decimal(quantity) / bought_volume) * Decimal("100"))
            if bought_volume > 0
            else ZERO
        )
        result = NestingResult(
            stock_id=stock.stock_id,
            stock_label=stock.label(),
            stock_form=stock.stock_form,
            stock_size=stock.label(),
            blanks_per_unit_stock=per_stock,
            qty_required=Decimal(pieces),
            unit_cost=_q2(stock.unit_cost),
            total_cost=total_cost,
            utilisation_pct=utilisation,
        )
        candidates.append(((total_cost, pieces, -utilisation, stock.stock_id), result))

    if not candidates:
        raise NestingError(
            "Part does not fit any available stock size — needs a buyer decision"
        )
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]
