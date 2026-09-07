"""Material densities, and starting prices for the materials this shop buys.

    python -m scripts.seed_materials

Two different kinds of number live here, and the difference is the whole
point:

**Densities are facts.** Steel is 7,850 kg/m3 because steel is 7,850 kg/m3.
They are physical constants, they do not drift, and pricing from them is
exact. They are seeded as data and never need refreshing.

**Prices are estimates, and are recorded as such.** They are ballpark UK
trade figures for small quantities — good enough to get a quote in the right
postcode, not good enough to send to a customer without a glance. Every one
is written with `method=manual` and evidence saying plainly that it was not
fetched from anywhere, so the workspace shows it as "not live" and a quote
built on it carries that mark.

That is deliberate. The rest of this system refuses to price from a
remembered number, and these are remembered numbers. Rather than smuggle
them in as though they were read off a supplier's page, they come in wearing
a label. Replace them by either:

  * giving each market source the address of a page that shows its price and
    running `scripts/refresh_market.py`, which reads the real figure and
    keeps the line of text it read it from; or
  * typing your own account prices in on the Market data screen, which is
    better still — a published web price is small-quantity retail, and buying
    on account you pay less.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.db import SessionLocal, init_db
from app.enums import MarketBasis, MarketKind, MarketMethod, MarketUnit
from app.models import MarketObservation, MarketSource, StockSize

#: kg/m3. Physical constants, not estimates.
DENSITY: dict[str, str] = {
    "S275": "7850",
    "EN3B": "7850",
    "EN8": "7850",
    "EN16": "7850",
    "EN16T": "7850",
    "EN24": "7850",
    "EN24T": "7850",
    "EN30B": "7860",
    "1.2312": "7850",
    "1.2379": "7700",
    "D2": "7700",
    "303": "8000",
    "304": "8000",
    "316": "8000",
    "6082-T6": "2700",
    "7075": "2810",
    "CZ121": "8470",
    "Copper": "8960",
}

#: Ballpark UK trade prices, £/kg, small quantity, ex-VAT.
#:
#: These are estimates from general market knowledge as at September 2026 —
#: NOT fetched, NOT quoted, and NOT this shop's account prices. They exist so
#: the calculator produces a number in the right region on day one instead of
#: refusing to price anything. Treat every one as a ceiling: on account, and
#: at any volume, EDM Zone will pay less.
ESTIMATED_PRICE: dict[str, tuple[str, str]] = {
    # spec: (£/kg, what it is)
    "S275": ("1.50", "mild steel, the cheapest thing on the rack"),
    "EN3B": ("1.60", "mild steel bright bar"),
    "EN8": ("1.90", "medium carbon, general purpose"),
    "EN16": ("2.50", "through-hardening, 605M36"),
    "EN16T": ("2.50", "605M36 supplied hardened and tempered"),
    "EN24": ("3.00", "high tensile, 817M40"),
    "EN24T": ("3.00", "817M40 supplied hardened and tempered"),
    "EN30B": ("5.25", "nickel alloy — several times the price of EN8"),
    "1.2312": ("5.00", "pre-toughened tool steel, plate"),
    "1.2379": ("6.50", "cold work tool steel"),
    "D2": ("6.50", "cold work tool steel"),
    "303": ("5.25", "free-machining stainless"),
    "304": ("5.50", "general stainless"),
    "316": ("7.00", "marine grade stainless"),
    "6082-T6": ("5.50", "structural aluminium"),
    "7075": ("9.00", "aerospace aluminium"),
    "CZ121": ("9.00", "free-machining brass"),
    "Copper": ("14.00", "electrode stock"),
}

EVIDENCE = (
    "ESTIMATE, not fetched. Ballpark UK trade price for a small quantity, "
    "ex-VAT, from general market knowledge in September 2026. Not a quote and "
    "not this shop's account price — treat it as a ceiling. Replace it by "
    "pointing this source at a supplier page, or by typing the account price."
)


def series_key(spec: str) -> str:
    return f"material:{spec.lower().replace(' ', '_')}"


def main() -> int:
    init_db()
    db = SessionLocal()
    added_sources = added_prices = priced_stock = 0

    try:
        for spec, (price, description) in ESTIMATED_PRICE.items():
            key = series_key(spec)
            source = db.query(MarketSource).filter_by(series_key=key).first()
            if source is None:
                source = MarketSource(
                    series_key=key,
                    name=f"{spec} — {description}",
                    kind=MarketKind.MATERIAL_PRICE.value,
                    unit=MarketUnit.GBP_PER_KG.value,
                    basis=MarketBasis.SURVEY.value,
                    spec=spec,
                    target="the price per kilogram, and every size stocked, in mm",
                    max_age_hours=168,
                    # Off: it has no URL, so a refresh would only fail. It
                    # switches itself on when somebody gives it an address.
                    active=False,
                )
                db.add(source)
                db.flush()
                added_sources += 1

            if db.query(MarketObservation).filter_by(series_key=key).count() == 0:
                db.add(
                    MarketObservation(
                        source_id=source.id,
                        series_key=key,
                        value=Decimal(price),
                        unit=MarketUnit.GBP_PER_KG.value,
                        # Manual, not ai_read: nobody read this off a page.
                        method=MarketMethod.MANUAL.value,
                        basis=MarketBasis.SURVEY.value,
                        # Above the floor, so the figure is usable: an
                        # estimate a shop can work from beats a blank that
                        # stops it quoting at all. It does not need hiding to
                        # be safe, because `method=manual` already makes the
                        # workspace render it as "not live" wherever it
                        # appears — visibly provisional, not invisible.
                        confidence=0.85,
                        evidence=EVIDENCE,
                        observed_at=datetime.now(UTC),
                    )
                )
                added_prices += 1

        # Any stock row for a material we know the density of can now be
        # costed from a per-kilo price rather than a typed figure.
        for row in db.query(StockSize):
            if row.spec in DENSITY and row.density_kg_m3 is None:
                row.density_kg_m3 = Decimal(DENSITY[row.spec])
                row.market_series_key = series_key(row.spec)
                priced_stock += 1

        db.commit()
    finally:
        db.close()

    print(f"\n  {added_sources} material source(s) added")
    print(f"  {added_prices} starting price(s) recorded")
    print(f"  {priced_stock} stock size(s) given a density\n")
    print("  Every price is recorded as an ESTIMATE. They are usable — a shop")
    print("  that cannot quote at all is worse off than one quoting from a")
    print("  ballpark — but the workspace renders each one as 'not live', so")
    print("  nobody mistakes it for a figure read off a supplier's page.\n")
    print("  To make them real: give each source the address of a page showing")
    print("  its price, then run  python -m scripts.refresh_market\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
