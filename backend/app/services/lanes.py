"""Which working list an enquiry belongs in.

The queue answers "what is in the system". It does not answer the question an
estimator actually asks, which is "what do I have to do next" — those are
different lists, and a single list sorted by age silently mixes an enquiry
that is blocked with one that is finished and waiting on the customer.

Two rules make this trustworthy rather than decorative:

  * **Every enquiry lands in exactly one lane.** Lanes are exhaustive and
    mutually exclusive, so nothing can fall between them. An enquiry that
    appears in no list is an enquiry nobody chases, which is the failure this
    whole system exists to prevent.
  * **Trouble outranks progress.** An enquiry with an unresolved blocking flag
    goes to Needs attention no matter how far along it is. A blocked quote
    sitting in "Ready to send" is exactly the one that gets sent by accident.

The counts on the tabs and the rows inside them come from this same function,
so a badge saying three and a list showing two is not possible.
"""

from __future__ import annotations

from enum import StrEnum

from app.enums import EnquiryStatus


class Lane(StrEnum):
    """The working lists, in the order they are worked."""

    #: Something is wrong or missing and a person has to deal with it.
    NEEDS_ATTENTION = "needs_attention"
    #: Arrived, being read and routed. No decision is owed yet.
    COMING_IN = "coming_in"
    #: Priced and waiting for an estimator to agree with it.
    TO_CHECK = "to_check"
    #: Approved. The draft exists and nobody has sent it.
    READY_TO_SEND = "ready_to_send"
    #: Sent. The ball is in the customer's court.
    AWAITING_FEEDBACK = "awaiting_feedback"
    #: Won or lost. Kept for calibration, out of the working day.
    CLOSED = "closed"


#: Order matters: this is the order the tabs appear, left to right, and it is
#: the order the work actually happens in.
LANE_ORDER: tuple[Lane, ...] = (
    Lane.NEEDS_ATTENTION,
    Lane.COMING_IN,
    Lane.TO_CHECK,
    Lane.READY_TO_SEND,
    Lane.AWAITING_FEEDBACK,
    Lane.CLOSED,
)

LANE_LABELS: dict[Lane, str] = {
    Lane.NEEDS_ATTENTION: "Needs attention",
    Lane.COMING_IN: "Coming in",
    Lane.TO_CHECK: "To check",
    Lane.READY_TO_SEND: "Ready to send",
    Lane.AWAITING_FEEDBACK: "Awaiting feedback",
    Lane.CLOSED: "Won / lost",
}

#: What each lane means, shown under the tab so nobody has to guess whether
#: "To check" means the AI checked it or somebody still has to.
LANE_HINTS: dict[Lane, str] = {
    Lane.NEEDS_ATTENTION: "blocked, failed, or waiting on a decision",
    Lane.COMING_IN: "arrived, being read and routed",
    Lane.TO_CHECK: "priced — an estimator needs to agree with the figure",
    Lane.READY_TO_SEND: "approved, draft written, not yet sent",
    Lane.AWAITING_FEEDBACK: "sent — waiting on the customer",
    Lane.CLOSED: "won or lost, kept for calibration",
}

#: Statuses that are off the happy path entirely.
_TROUBLE = frozenset({EnquiryStatus.NEEDS_ATTENTION.value, EnquiryStatus.FAILED.value})
_CLOSED = frozenset({EnquiryStatus.WON.value, EnquiryStatus.LOST.value})
_EARLY = frozenset(
    {
        EnquiryStatus.RECEIVED.value,
        EnquiryStatus.EXTRACTING.value,
        EnquiryStatus.EXTRACTED.value,
        EnquiryStatus.CLASSIFIED.value,
    }
)
_PRICED = frozenset({EnquiryStatus.PRICED.value, EnquiryStatus.IN_REVIEW.value})


def lane_for(status: str, *, blocking_flag_count: int = 0) -> Lane:
    """The one lane this enquiry belongs in.

    `blocking_flag_count` is unresolved blockers only. A resolved flag is a
    problem somebody has already dealt with, and holding the enquiry back for
    it would train people to ignore the lane.
    """
    if status in _TROUBLE:
        return Lane.NEEDS_ATTENTION
    if status in _CLOSED:
        # A won job's leftover flag is history, not work. Closed wins here.
        return Lane.CLOSED
    if blocking_flag_count > 0:
        return Lane.NEEDS_ATTENTION
    if status == EnquiryStatus.SENT.value:
        return Lane.AWAITING_FEEDBACK
    if status == EnquiryStatus.APPROVED.value:
        return Lane.READY_TO_SEND
    if status in _PRICED:
        return Lane.TO_CHECK
    if status in _EARLY:
        return Lane.COMING_IN
    # A status nobody has taught this function about is not silently dropped
    # into a lane where it will be ignored. It surfaces where it gets looked at.
    return Lane.NEEDS_ATTENTION


def statuses_in(lane: Lane) -> frozenset[str]:
    """Statuses that *can* appear in a lane, for narrowing a query.

    Only a pre-filter: an enquiry with a blocking flag can be pulled out of
    any of these into Needs attention, so `lane_for` still decides. Needs
    attention can hold any status at all, which is why it lists them all.
    """
    if lane is Lane.NEEDS_ATTENTION:
        return frozenset(s.value for s in EnquiryStatus)
    if lane is Lane.CLOSED:
        return frozenset(_CLOSED)
    if lane is Lane.AWAITING_FEEDBACK:
        return frozenset({EnquiryStatus.SENT.value})
    if lane is Lane.READY_TO_SEND:
        return frozenset({EnquiryStatus.APPROVED.value})
    if lane is Lane.TO_CHECK:
        return frozenset(_PRICED)
    return frozenset(_EARLY)
