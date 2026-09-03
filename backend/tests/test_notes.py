"""Tests for the note → reprice loop.

The behaviour under test is mostly *refusal*: what the model is not allowed to
do to a quote, and what happens to the price when it tries.
"""

from decimal import Decimal as D

import pytest

from app.enums import NoteKind, Process, TimeSource
from app.models import Flag, RulesTable
from app.services.ai import AIUnavailable, StubAIClient
from app.services.notes import NoteError, add_note, recurring_note_candidates
from app.services.quoting import price_enquiry


def action(action_name: str, **kw) -> dict:
    base = {
        "action": action_name,
        "part_id": None,
        "op_number": None,
        "process": None,
        "description": None,
        "set_time_mins": None,
        "run_time_mins_per_unit": None,
        "field_name": None,
        "field_value": None,
        "margin_pct": None,
        "rule_key": None,
        "question": None,
        "reason": "because the note said so",
    }
    base.update(kw)
    return base


def payload(*actions, kind=NoteKind.FACT_CORRECTION.value, summary="did a thing") -> dict:
    return {"note_kind": kind, "summary": summary, "actions": list(actions)}


@pytest.fixture
def quoted(db, enquiry, priceable_part, rates, rules):
    quote = price_enquiry(db, enquiry)
    db.commit()
    return enquiry, quote, priceable_part


# --------------------------------------------------------------------------
# The engine, not the model, produces the price
# --------------------------------------------------------------------------
def test_a_fact_correction_changes_an_input_and_the_engine_reprices(quoted, db):
    enquiry, quote, part = quoted
    before = D(quote.quote_value)
    stub = StubAIClient(
        [
            payload(
                action("set_operation_time", part_id=part.id, op_number=10, set_time_mins=30),
                summary="removed 15m of electrode setup",
            )
        ]
    )
    note = add_note(
        db, enquiry, quote, note_text="We already have the electrode, drop 15 minutes of setup.",
        author="est@shop.example", ai=stub,
    )
    db.commit()

    assert note.note_kind == NoteKind.FACT_CORRECTION.value
    assert note.adjustment_summary == "removed 15m of electrode setup"
    assert note.price_before == before
    assert note.price_after < before
    # The stored price is the engine's, recomputed from the changed input.
    assert note.price_after == D(quote.quote_value)
    operation = next(op for op in part.operations if op.op_number == 10)
    assert operation.set_time_mins == D("30")
    # A number an estimator gave is a manual time, not a calculator output.
    assert operation.time_source == TimeSource.MANUAL.value


def test_removing_an_operation_lowers_the_price(quoted, db):
    enquiry, quote, part = quoted
    before = D(quote.quote_value)
    stub = StubAIClient([payload(action("remove_operation", part_id=part.id, op_number=20))])
    note = add_note(db, enquiry, quote, note_text="No wire needed, we can mill it.",
                    author="est@shop.example", ai=stub)
    db.commit()
    assert note.price_after < before
    assert 20 not in [op.op_number for op in part.operations]


def test_the_note_is_recorded_even_when_it_implies_no_change(quoted, db):
    enquiry, quote, part = quoted
    before = D(quote.quote_value)
    stub = StubAIClient([payload(summary="No change implied — context only.")])
    note = add_note(db, enquiry, quote, note_text="This customer is always slow to respond.",
                    author="est@shop.example", ai=stub)
    db.commit()
    assert note.id is not None
    assert note.price_before == note.price_after == before


def test_an_empty_note_is_rejected(quoted, db):
    enquiry, quote, _ = quoted
    with pytest.raises(NoteError):
        add_note(db, enquiry, quote, note_text="   ", author="est@shop.example",
                 ai=StubAIClient([payload()]))


# --------------------------------------------------------------------------
# Percentages come from rules_table, never from the model
# --------------------------------------------------------------------------
def test_a_rule_the_business_defined_can_be_applied(quoted, db, rules):
    enquiry, quote, _ = quoted
    before = D(quote.quote_value)
    stub = StubAIClient(
        [payload(action("apply_rule", rule_key="rush_uplift"),
                 kind=NoteKind.COMMERCIAL_INSTRUCTION.value)]
    )
    note = add_note(db, enquiry, quote, note_text="They need this Friday, add the rush.",
                    author="est@shop.example", ai=stub)
    db.commit()
    assert note.applied_rule_id == rules["rush_uplift"].id
    assert note.price_after > before
    assert rules["rush_uplift"].id in (quote.applied_rule_ids or [])


def test_an_undefined_rule_is_refused_and_becomes_a_question(quoted, db):
    enquiry, quote, _ = quoted
    before = D(quote.quote_value)
    # A rule_key the business has never defined.
    stub = StubAIClient([payload(action("apply_rule", rule_key="loyalty_discount"))])
    note = add_note(db, enquiry, quote, note_text="Give them something off, they're a good customer.",
                    author="est@shop.example", ai=stub)
    db.commit()

    assert note.price_after == before, "an undefined rule must not move the price"
    assert note.awaiting_answer is True
    assert "loyalty_discount" in note.question
    assert note.applied_rule_id is None
    assert note.proposed_change["rejected"], "the refused action must be recorded, not dropped"


def test_an_inactive_rule_is_refused(quoted, db, rules):
    enquiry, quote, _ = quoted
    rules["rush_uplift"].active = False
    db.commit()
    before = D(quote.quote_value)
    stub = StubAIClient([payload(action("apply_rule", rule_key="rush_uplift"))])
    note = add_note(db, enquiry, quote, note_text="Add the rush.", author="est@shop.example", ai=stub)
    db.commit()
    assert note.price_after == before
    assert note.awaiting_answer is True


def test_applying_a_rule_with_no_key_asks_rather_than_picking_a_number(quoted, db):
    enquiry, quote, _ = quoted
    before = D(quote.quote_value)
    stub = StubAIClient([payload(action("apply_rule", rule_key=None))])
    note = add_note(db, enquiry, quote, note_text="Add a bit of contingency.",
                    author="est@shop.example", ai=stub)
    db.commit()
    assert note.price_after == before
    assert note.awaiting_answer is True
    assert "rule" in note.question.lower()


def test_only_the_active_rule_keys_are_offered_to_the_model(quoted, db, rules):
    enquiry, quote, _ = quoted
    rules["rush_uplift"].active = False
    db.commit()
    stub = StubAIClient([payload()])
    add_note(db, enquiry, quote, note_text="anything", author="est@shop.example", ai=stub)
    schema = stub.calls[0]["schema"]
    offered = schema["properties"]["actions"]["items"]["properties"]["rule_key"]["enum"]
    assert "rush_uplift" not in offered
    assert "min_quote_value" in offered


# --------------------------------------------------------------------------
# Other refusals
# --------------------------------------------------------------------------
def test_a_margin_outside_0_to_100_is_refused(quoted, db):
    enquiry, quote, _ = quoted
    before = D(quote.margin_pct)
    stub = StubAIClient([payload(action("set_margin_pct", margin_pct=250))])
    note = add_note(db, enquiry, quote, note_text="Triple it.", author="est@shop.example", ai=stub)
    db.commit()
    assert quote.margin_pct == before
    assert note.awaiting_answer is True


def test_a_margin_the_estimator_stated_is_applied(quoted, db):
    enquiry, quote, _ = quoted
    stub = StubAIClient(
        [payload(action("set_margin_pct", margin_pct=20), kind=NoteKind.COMMERCIAL_INSTRUCTION.value)]
    )
    add_note(db, enquiry, quote, note_text="Quote this one at 20% margin.",
             author="est@shop.example", ai=stub)
    db.commit()
    assert quote.margin_pct == D("20")


def test_an_operation_time_with_no_number_asks_instead_of_guessing(quoted, db):
    enquiry, quote, part = quoted
    before = D(quote.quote_value)
    stub = StubAIClient([payload(action("set_operation_time", part_id=part.id, op_number=10))])
    note = add_note(db, enquiry, quote, note_text="That mill setup looks high to me.",
                    author="est@shop.example", ai=stub)
    db.commit()
    assert note.price_after == before
    assert note.awaiting_answer is True
    assert "op 10" in note.question


def test_a_negative_time_is_refused(quoted, db):
    enquiry, quote, part = quoted
    stub = StubAIClient(
        [payload(action("set_operation_time", part_id=part.id, op_number=10, set_time_mins=-30))]
    )
    add_note(db, enquiry, quote, note_text="Take an hour off.", author="est@shop.example", ai=stub)
    db.commit()
    assert next(op for op in part.operations if op.op_number == 10).set_time_mins == D("45")


def test_an_unknown_process_is_refused(quoted, db):
    enquiry, quote, part = quoted
    stub = StubAIClient(
        [payload(action("add_operation", part_id=part.id, op_number=40, process="laser_cut"))]
    )
    add_note(db, enquiry, quote, note_text="Laser it.", author="est@shop.example", ai=stub)
    db.commit()
    assert 40 not in [op.op_number for op in part.operations]


def test_an_added_operation_with_no_times_blocks_rather_than_costing_zero(quoted, db):
    enquiry, quote, part = quoted
    stub = StubAIClient(
        [payload(action("add_operation", part_id=part.id, op_number=40,
                        process=Process.GRIND.value, description="Grind the face"))]
    )
    note = add_note(db, enquiry, quote, note_text="It needs grinding after heat treat.",
                    author="est@shop.example", ai=stub)
    db.commit()
    assert 40 in [op.op_number for op in part.operations]
    assert note.awaiting_answer is True
    blocker = db.query(Flag).filter(Flag.dedupe_key == "op_no_times:40").one()
    assert blocker.severity == "block"


def test_an_uneditable_part_field_is_refused(quoted, db):
    enquiry, quote, part = quoted
    stub = StubAIClient(
        [payload(action("set_part_field", part_id=part.id, field_name="drawing_number",
                        field_value="9999"))]
    )
    add_note(db, enquiry, quote, note_text="Change the drawing number.",
             author="est@shop.example", ai=stub)
    db.commit()
    assert part.drawing_number == "4471"


def test_a_failed_interpretation_still_records_the_note(quoted, db):
    enquiry, quote, _ = quoted
    before = D(quote.quote_value)
    note = add_note(db, enquiry, quote, note_text="We already have the fixture.",
                    author="est@shop.example", ai=StubAIClient([]))  # raises AIUnavailable
    db.commit()
    assert note.id is not None
    assert note.note_text == "We already have the fixture."
    assert note.awaiting_answer is True
    assert note.price_after == before
    assert "by hand" in note.question


# --------------------------------------------------------------------------
# Promotion is a human act
# --------------------------------------------------------------------------
def test_recurring_notes_are_only_suggested_never_promoted(quoted, db):
    enquiry, quote, part = quoted
    for _ in range(3):
        stub = StubAIClient([payload(summary="added contingency for drawing churn")])
        add_note(db, enquiry, quote, note_text="They always change the drawing.",
                 author="est@shop.example", ai=stub)
    db.commit()

    candidates = recurring_note_candidates(db, minimum=3)
    assert candidates and candidates[0]["occurrences"] == 3
    # Suggesting a rule must not have created one.
    assert db.query(RulesTable).filter(
        RulesTable.promoted_from_note_id.is_not(None)
    ).count() == 0
