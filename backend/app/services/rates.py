"""Rate and rule lookup.

"Rates come from rate_table by process + effective date. Never hardcoded,
never AI-generated." (spec stage 4)

There is no default rate anywhere in this module. A process with no effective
rate row raises, which becomes a blocking flag — because the alternative is
quoting a customer at a number nobody in the business chose.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.enums import AdjustmentType, RuleKey
from app.models import RateTable, RulesTable, StockSize
from app.pricing import AdjustmentRule, MissingRate


def resolve_rate(
    db: Session,
    process: str,
    *,
    machine_group: str | None = None,
    on_date: date | None = None,
) -> RateTable:
    """The rate in force for a process on a date.

    A row naming the requested machine group wins over a general row for the
    process; among equally specific rows the latest `effective_from` wins, and
    the highest id breaks a same-day tie so the answer is deterministic.
    """
    on_date = on_date or date.today()
    stmt = (
        select(RateTable)
        .where(
            RateTable.process == process,
            RateTable.effective_from <= on_date,
            or_(RateTable.effective_to.is_(None), RateTable.effective_to >= on_date),
        )
        .order_by(RateTable.effective_from.desc(), RateTable.id.desc())
    )
    candidates = list(db.scalars(stmt).all())
    if not candidates:
        raise MissingRate(process, machine_group)

    if machine_group:
        specific = [r for r in candidates if r.machine_group == machine_group]
        if specific:
            return specific[0]
    general = [r for r in candidates if r.machine_group is None]
    return (general or candidates)[0]


def rate_history(db: Session, process: str) -> list[RateTable]:
    return list(
        db.scalars(
            select(RateTable)
            .where(RateTable.process == process)
            .order_by(RateTable.effective_from.desc())
        ).all()
    )


def active_rules(db: Session) -> list[RulesTable]:
    return list(
        db.scalars(
            select(RulesTable).where(RulesTable.active.is_(True)).order_by(RulesTable.id)
        ).all()
    )


def rule_by_key(db: Session, rule_key: str) -> RulesTable | None:
    """The active rule for a key, or None.

    The note loop calls this before proposing a percentage. None means the AI
    must ask the estimator rather than pick a number (spec stage 5).
    """
    return db.scalars(
        select(RulesTable)
        .where(RulesTable.rule_key == rule_key, RulesTable.active.is_(True))
        .order_by(RulesTable.id.desc())
    ).first()


def to_adjustment_rule(row: RulesTable) -> AdjustmentRule:
    return AdjustmentRule(
        rule_id=row.id,
        rule_key=row.rule_key,
        adjustment_type=row.adjustment_type,
        adjustment_value=Decimal(row.adjustment_value),
        trigger_description=row.trigger_description,
    )


def rules_in_scope(db: Session, applied_rule_ids: list[int] | None) -> list[AdjustmentRule]:
    """The rules the engine should apply to a quote.

    Two sources, deliberately separate:

    * ``min_quote_value`` — a standing floor that applies to every quote
      whether anyone selected it or not;
    * the rules a human (or the note loop, citing a specific row) put in scope
      for this quote.

    Nothing else is applied automatically. A rule that fires on its own without
    someone choosing it is how a quote acquires an uplift nobody can explain.
    """
    rules: list[AdjustmentRule] = []
    seen: set[int] = set()

    floor = rule_by_key(db, RuleKey.MIN_QUOTE_VALUE.value)
    if floor is not None and floor.adjustment_type != AdjustmentType.FLAG_ONLY.value:
        rules.append(to_adjustment_rule(floor))
        seen.add(floor.id)

    for rule_id in applied_rule_ids or []:
        if rule_id in seen:
            continue
        row = db.get(RulesTable, rule_id)
        if row is None or not row.active:
            continue
        rules.append(to_adjustment_rule(row))
        seen.add(row.id)

    return rules


def stock_options(db: Session, spec: str) -> list[StockSize]:
    """Active stock rows whose spec matches, for the nesting calculator."""
    return list(
        db.scalars(
            select(StockSize)
            .where(StockSize.active.is_(True), StockSize.spec == spec)
            .order_by(StockSize.id)
        ).all()
    )
