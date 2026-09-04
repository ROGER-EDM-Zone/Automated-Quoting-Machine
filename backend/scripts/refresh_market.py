"""Refresh the live market data, and say plainly what happened.

    python -m scripts.refresh_market              # refresh everything
    python -m scripts.refresh_market --show       # just show what is known
    python -m scripts.refresh_market --series material:en16:round_bar
    python -m scripts.refresh_market --seed       # write the starting sources

This is the thing to put on a schedule — a Windows scheduled task or a cron
entry, once a night is plenty for steel — so the quotes going out on Tuesday
are not priced on last month's figures.

It reports per source, because "the refresh ran" is not the useful fact. The
useful fact is which of your suppliers answered, which did not, and how old
the number is that a quote would use right now.
"""

from __future__ import annotations

import argparse

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.enums import MarketBasis, MarketKind, MarketUnit, StockForm
from app.models import MarketSource
from app.services import market

TICK = "  \033[32m✓\033[0m"
CROSS = "  \033[31m✗\033[0m"
WARN = "  \033[33m!\033[0m"
INFO = "  \033[2m·\033[0m"


def dim(text: str) -> str:
    return f"\033[2m{text}\033[0m"


#: A starting set covering everything a quote leans on that the business does
#: not itself decide. Every one needs a URL that actually shows the number —
#: they are left blank deliberately, because a URL guessed by this script is
#: the same failure as a price guessed by the model.
SEED_SOURCES = [
    {
        "series_key": "material:en16:round_bar",
        "name": "EN16/605M36 round bar",
        "kind": MarketKind.MATERIAL_PRICE.value,
        "unit": MarketUnit.GBP_PER_KG.value,
        "basis": MarketBasis.RETAIL_ONLINE.value,
        "spec": "EN16",
        "stock_form": StockForm.BAR_ROUND.value,
        "target": "the price per kilogram, and every diameter listed, in mm",
        "max_age_hours": 168,
    },
    {
        "series_key": "material:en30b:square_bar",
        "name": "EN30B square bar",
        "kind": MarketKind.MATERIAL_PRICE.value,
        "unit": MarketUnit.GBP_PER_KG.value,
        "basis": MarketBasis.RETAIL_ONLINE.value,
        "spec": "EN30B",
        "stock_form": StockForm.BAR_SQUARE.value,
        "target": "the price per kilogram, and every square section listed, in mm",
        "max_age_hours": 168,
    },
    {
        "series_key": "material:1.2312:plate",
        "name": "1.2312 tool steel plate",
        "kind": MarketKind.MATERIAL_PRICE.value,
        "unit": MarketUnit.GBP_PER_KG.value,
        "basis": MarketBasis.RETAIL_ONLINE.value,
        "spec": "1.2312",
        "stock_form": StockForm.PLATE.value,
        "target": "the price per kilogram, and every plate thickness listed, in mm",
        "max_age_hours": 168,
    },
    {
        "series_key": "material:aluminium:6082",
        "name": "6082-T6 aluminium bar",
        "kind": MarketKind.MATERIAL_PRICE.value,
        "unit": MarketUnit.GBP_PER_KG.value,
        "basis": MarketBasis.RETAIL_ONLINE.value,
        "spec": "6082-T6",
        "stock_form": StockForm.BAR_ROUND.value,
        "target": "the price per kilogram, and every diameter listed, in mm",
        "max_age_hours": 168,
    },
    {
        "series_key": "material:stainless:316",
        "name": "316 stainless bar",
        "kind": MarketKind.MATERIAL_PRICE.value,
        "unit": MarketUnit.GBP_PER_KG.value,
        "basis": MarketBasis.RETAIL_ONLINE.value,
        "spec": "316",
        "stock_form": StockForm.BAR_ROUND.value,
        "target": "the price per kilogram, and every diameter listed, in mm",
        "max_age_hours": 168,
    },
    {
        "series_key": "consumable:edm_wire",
        "name": "Wire EDM brass wire",
        "kind": MarketKind.CONSUMABLE.value,
        "unit": MarketUnit.GBP_EACH.value,
        "basis": MarketBasis.RETAIL_ONLINE.value,
        "target": "the price of one spool of 0.25mm brass EDM wire",
        "max_age_hours": 720,
    },
    {
        "series_key": "energy:uk_business_electricity",
        "name": "UK business electricity unit rate",
        "kind": MarketKind.ENERGY.value,
        "unit": MarketUnit.GBP_PER_KWH.value,
        "basis": MarketBasis.SURVEY.value,
        "target": "the average business electricity unit rate in pence or pounds per kWh",
        # Tariffs move quarterly, not daily.
        "max_age_hours": 2160,
    },
    {
        "series_key": "labour:uk_subcontract_machining",
        "name": "UK subcontract machining hourly rate",
        "kind": MarketKind.LABOUR_RATE.value,
        "unit": MarketUnit.GBP_PER_HOUR.value,
        "basis": MarketBasis.SURVEY.value,
        "target": (
            "the typical hourly charge-out rate for UK subcontract CNC "
            "machining, and the range if one is given"
        ),
        # A benchmark, never a rate. It informs what the shop charges; it does
        # not set it. Rates stay in the rate table where a human owns them.
        "max_age_hours": 2160,
    },
]


def seed(db) -> int:
    existing = {row.series_key for row in db.query(MarketSource).all()}
    added = 0
    for spec in SEED_SOURCES:
        if spec["series_key"] in existing:
            continue
        # Inactive until somebody gives it a URL: an active source with
        # nowhere to look just fails every night and trains people to ignore
        # the failures.
        db.add(MarketSource(**spec, active=False))
        added += 1
    db.commit()

    print(f"\n{TICK} Added {added} source(s); {len(existing)} already there.\n")
    if added:
        print(
            "  Each one needs the address of a page that actually shows the\n"
            "  number, and then switching on. Until then it stays off and\n"
            "  nothing prices from it.\n\n"
            "  Set the URL in the app under Admin -> Market data, or with:\n"
            "    PATCH /api/admin/market/sources/{id}\n"
        )
    return 0


def show(db) -> int:
    rows = market.series_summary(db)
    if not rows:
        print("\nNo market sources configured. Run with --seed to start.\n")
        return 1

    print(f"\n{'Series':<38}{'Value':>12}  {'Age':<12}{'Status'}")
    print("-" * 78)
    for row in rows:
        value = f"{row['value']} {row['unit']}" if row["value"] else "—"
        if row["age_hours"] is None:
            age = "never"
        elif row["age_hours"] < 24:
            age = f"{row['age_hours']:.0f}h"
        else:
            age = f"{row['age_hours'] / 24:.0f}d"
        marker = {
            "current": TICK,
            "stale": WARN,
            "never_read": CROSS,
            "last_refresh_failed": CROSS,
            "off": INFO,
        }.get(row["status"], INFO)
        print(f"{row['series_key']:<38}{value:>12}  {age:<12}{marker} {row['status']}")
        if row["evidence"]:
            print(f"    {dim(row['evidence'][:70])}")
        if row["last_error"]:
            print(f"    {dim('last error: ' + row['last_error'][:66])}")
    print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series", help="refresh only this series key")
    parser.add_argument("--show", action="store_true", help="show what is known, refresh nothing")
    parser.add_argument("--seed", action="store_true", help="write the starting set of sources")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        if args.seed:
            return seed(db)
        if args.show:
            return show(db)

        settings = get_settings()
        if not settings.anthropic_api_key:
            print(
                "\nAQM_ANTHROPIC_API_KEY is not set, so pages cannot be read.\n"
                "Set it in backend/.env and run this again.\n"
            )
            return 1

        print("\nRefreshing market data\n")
        report = market.refresh(db, series_key=args.series)
        db.commit()

        if not report.results:
            print(
                f"{WARN} No active sources to refresh."
                " Run with --seed, then give each one a URL and switch it on.\n"
            )
            return 1

        for result in report.results:
            if result.ok:
                print(f"{TICK} {result.series_key}: {result.detail}")
                if result.stock_rows_written:
                    print(f"    {dim(f'{result.stock_rows_written} new stock size(s) added')}")
            else:
                print(f"{CROSS} {result.series_key}: {result.detail}")

        print(f"\n{len(report.succeeded)} succeeded, {len(report.failed)} failed.\n")
        return 0 if report.all_ok else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
