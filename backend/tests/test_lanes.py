"""Working lists.

The tabs are only worth having if they are exhaustive. An enquiry that
belongs in no list is an enquiry nobody chases, which is a worse failure than
having no tabs at all — so most of what is tested here is that nothing can
fall between them.
"""

import pytest

from app.enums import EnquiryStatus, FlagCategory, FlagSeverity
from app.models import Enquiry, Flag, utcnow
from app.services.lanes import (
    LANE_HINTS,
    LANE_LABELS,
    LANE_ORDER,
    Lane,
    lane_for,
    statuses_in,
)


# --------------------------------------------------------------------------
# Exhaustive and exclusive
# --------------------------------------------------------------------------
@pytest.mark.parametrize("status", [s.value for s in EnquiryStatus])
def test_every_status_lands_in_a_lane(status):
    assert lane_for(status) in LANE_ORDER


@pytest.mark.parametrize("status", [s.value for s in EnquiryStatus])
def test_every_status_still_lands_in_a_lane_when_blocked(status):
    assert lane_for(status, blocking_flag_count=3) in LANE_ORDER


def test_a_status_nobody_taught_it_about_surfaces_rather_than_vanishing():
    # The dangerous failure is a new status quietly landing in a lane nobody
    # reads. It goes where someone will look at it instead.
    assert lane_for("some_status_added_later") is Lane.NEEDS_ATTENTION


def test_every_lane_has_a_label_and_a_hint():
    for lane in LANE_ORDER:
        assert LANE_LABELS[lane]
        assert LANE_HINTS[lane]


def test_the_lanes_are_listed_in_the_order_work_happens():
    assert LANE_ORDER[0] is Lane.NEEDS_ATTENTION
    assert LANE_ORDER[-1] is Lane.CLOSED


# --------------------------------------------------------------------------
# Trouble outranks progress
# --------------------------------------------------------------------------
def test_a_blocked_quote_leaves_ready_to_send():
    assert lane_for(EnquiryStatus.APPROVED.value) is Lane.READY_TO_SEND
    # This is the one that matters: an approved quote with an unresolved
    # blocker must not sit in the list people send from.
    assert lane_for(EnquiryStatus.APPROVED.value, blocking_flag_count=1) is Lane.NEEDS_ATTENTION


def test_a_blocked_priced_enquiry_leaves_to_check():
    assert lane_for(EnquiryStatus.PRICED.value, blocking_flag_count=1) is Lane.NEEDS_ATTENTION


def test_a_won_job_stays_closed_even_with_a_stale_flag_on_it():
    # The work is done. A leftover flag is history, not a job to chase.
    assert lane_for(EnquiryStatus.WON.value, blocking_flag_count=2) is Lane.CLOSED
    assert lane_for(EnquiryStatus.LOST.value, blocking_flag_count=2) is Lane.CLOSED


def test_the_pipeline_maps_the_way_an_estimator_would_expect():
    assert lane_for(EnquiryStatus.RECEIVED.value) is Lane.COMING_IN
    assert lane_for(EnquiryStatus.CLASSIFIED.value) is Lane.COMING_IN
    assert lane_for(EnquiryStatus.PRICED.value) is Lane.TO_CHECK
    assert lane_for(EnquiryStatus.IN_REVIEW.value) is Lane.TO_CHECK
    assert lane_for(EnquiryStatus.APPROVED.value) is Lane.READY_TO_SEND
    assert lane_for(EnquiryStatus.SENT.value) is Lane.AWAITING_FEEDBACK
    assert lane_for(EnquiryStatus.FAILED.value) is Lane.NEEDS_ATTENTION


# --------------------------------------------------------------------------
# The query pre-filter must not be narrower than the lane
# --------------------------------------------------------------------------
@pytest.mark.parametrize("lane", LANE_ORDER)
def test_the_prefilter_never_excludes_a_status_the_lane_would_accept(lane):
    """`statuses_in` narrows a query; `lane_for` decides. If the narrowing is
    tighter than the decision, rows silently disappear from the tab."""
    allowed = statuses_in(lane)
    for status in (s.value for s in EnquiryStatus):
        for blockers in (0, 1):
            if lane_for(status, blocking_flag_count=blockers) is lane:
                assert status in allowed, (
                    f"{status} (blockers={blockers}) belongs in {lane} but "
                    "the query filter would exclude it"
                )


# --------------------------------------------------------------------------
# Through the API, against real rows
# --------------------------------------------------------------------------
def make_enquiry(db, status, *, subject, blocked=False):
    enquiry = Enquiry(
        outlook_message_id=f"lane-test-{subject}",
        subject=subject,
        status=status,
        received_at=utcnow(),
        tagged_at=utcnow(),
    )
    db.add(enquiry)
    db.flush()
    if blocked:
        db.add(
            Flag(
                enquiry_id=enquiry.id,
                category=FlagCategory.COMMERCIAL_JUDGEMENT.value,
                severity=FlagSeverity.BLOCK.value,
                message="Needs a decision before this can go out",
            )
        )
    db.commit()
    return enquiry


@pytest.fixture
def spread(api):
    """One enquiry in every lane, plus an approved-but-blocked one."""
    client, db, *_ = api
    make_enquiry(db, EnquiryStatus.RECEIVED.value, subject="just arrived")
    make_enquiry(db, EnquiryStatus.PRICED.value, subject="priced, unchecked")
    make_enquiry(db, EnquiryStatus.APPROVED.value, subject="ready to go")
    make_enquiry(db, EnquiryStatus.SENT.value, subject="out with the customer")
    make_enquiry(db, EnquiryStatus.WON.value, subject="won it")
    make_enquiry(db, EnquiryStatus.FAILED.value, subject="fell over")
    make_enquiry(db, EnquiryStatus.APPROVED.value, subject="approved but blocked", blocked=True)
    return client


def lane_counts(client):
    return {row["lane"]: row["count"] for row in client.get("/queue/lanes").json()}


def test_the_counts_total_every_enquiry_in_the_system(spread):
    counts = lane_counts(spread)
    assert sum(counts.values()) == 7


def test_each_lane_holds_what_it_says_it_holds(spread):
    counts = lane_counts(spread)
    assert counts[Lane.COMING_IN.value] == 1
    assert counts[Lane.TO_CHECK.value] == 1
    assert counts[Lane.AWAITING_FEEDBACK.value] == 1
    assert counts[Lane.CLOSED.value] == 1
    # One approved and clean; the blocked one has moved out.
    assert counts[Lane.READY_TO_SEND.value] == 1
    # The failure, plus the approved-but-blocked one.
    assert counts[Lane.NEEDS_ATTENTION.value] == 2


@pytest.mark.parametrize("lane", [lane.value for lane in LANE_ORDER])
def test_a_tabs_count_matches_the_rows_it_shows(spread, lane):
    """The invariant worth having a test for. A badge saying three over a
    list of two is how people stop trusting the screen."""
    counted = lane_counts(spread)[lane]
    rows = spread.get(f"/queue?lane={lane}").json()
    assert len(rows) == counted
    assert all(row["lane"] == lane for row in rows)


def test_asking_for_a_closed_lane_returns_closed_work(spread):
    # Without this, clicking "Won / lost" would show an empty list, because
    # the queue hides closed enquiries by default.
    rows = spread.get(f"/queue?lane={Lane.CLOSED.value}").json()
    assert [row["subject"] for row in rows] == ["won it"]

    sent = spread.get(f"/queue?lane={Lane.AWAITING_FEEDBACK.value}").json()
    assert [row["subject"] for row in sent] == ["out with the customer"]


def test_the_blocked_quote_is_in_needs_attention_not_ready_to_send(spread):
    ready = spread.get(f"/queue?lane={Lane.READY_TO_SEND.value}").json()
    assert [row["subject"] for row in ready] == ["ready to go"]

    attention = {row["subject"] for row in spread.get("/queue?lane=needs_attention").json()}
    assert "approved but blocked" in attention


def test_every_enquiry_appears_in_exactly_one_lane(spread):
    seen: dict[int, str] = {}
    for lane in LANE_ORDER:
        for row in spread.get(f"/queue?lane={lane.value}").json():
            assert row["enquiry_id"] not in seen, (
                f"enquiry {row['enquiry_id']} is in both {seen.get(row['enquiry_id'])} "
                f"and {lane.value}"
            )
            seen[row["enquiry_id"]] = lane.value
    assert len(seen) == 7


def test_an_unknown_lane_is_rejected_rather_than_silently_ignored(spread):
    response = spread.get("/queue?lane=ready_to_sned")
    assert response.status_code == 422
    assert "ready_to_send" in response.json()["detail"]


def test_the_plain_queue_still_works_and_carries_the_lane(spread):
    rows = spread.get("/queue").json()
    assert rows
    assert all("lane" in row for row in rows)
