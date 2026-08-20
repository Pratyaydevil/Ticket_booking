"""
Waitlist engine (evaluation focus — auto-assignment + time-limited offers).

Queue      : WaitlistEntry rows, FIFO per (event, category) by created_at.
Offer      : when a booked seat frees up, the seat is re-HELD for the head of
             the queue with hold_kind='waitlist_offer' and a secret token;
             the customer gets an emailed link valid for OFFER_TTL minutes.
Expiry     : the sweeper marks lapsed offers EXPIRED and calls
             offer_seat_to_next() again — the same seat cascades down the
             queue until someone books it or the queue empties.
"""
import secrets
from datetime import timedelta

from sqlalchemy.orm import Session

from ..config import WAITLIST_OFFER_TTL_SECONDS
from ..database import utcnow
from ..models import (EventSeat, HoldKind, SeatStatus, WaitlistEntry,
                      WaitlistStatus)


def offer_seat_to_next(db: Session, seat: EventSeat):
    """
    Route one freed seat to the waitlist.
    Returns the WaitlistEntry that received the offer (caller emails it after
    commit), or None if the queue is empty (seat simply becomes available).
    NOTE: mutates objects on the caller's session; caller commits.
    """
    head = (
        db.query(WaitlistEntry)
        .filter(WaitlistEntry.event_id == seat.event_id,
                WaitlistEntry.category == seat.seat.category,
                WaitlistEntry.status == WaitlistStatus.WAITING)
        .order_by(WaitlistEntry.created_at, WaitlistEntry.id)  # strict FIFO
        .first()
    )

    if head is None:
        # Nobody waiting -> seat goes back on open sale.
        seat.status = SeatStatus.AVAILABLE
        seat.held_by_id = None
        seat.hold_kind = None
        seat.hold_expires_at = None
        return None

    expires = utcnow() + timedelta(seconds=WAITLIST_OFFER_TTL_SECONDS)
    # Reserve the seat for exactly this customer while the offer is live —
    # nobody else can grab it, satisfying "offered to the next customer".
    seat.status = SeatStatus.HELD
    seat.held_by_id = head.customer_id
    seat.hold_kind = HoldKind.WAITLIST_OFFER
    seat.hold_expires_at = expires

    head.status = WaitlistStatus.OFFERED
    head.offer_token = secrets.token_urlsafe(24)   # unguessable link token
    head.offer_expires_at = expires
    head.offered_event_seat_id = seat.id
    # Flush now: when several seats free at once (multi-seat cancellation),
    # the next head-of-queue query must see this entry as OFFERED, otherwise
    # the same customer would be offered every seat.
    db.flush()
    return head


def expire_lapsed_offers(db: Session):
    """
    Sweeper job 2: for every offer past its deadline —
    mark it EXPIRED, then cascade the same seat to the next person in line.
    Returns fresh offers for the sweeper to email.
    """
    now = utcnow()
    lapsed = (
        db.query(WaitlistEntry)
        .filter(WaitlistEntry.status == WaitlistStatus.OFFERED,
                WaitlistEntry.offer_expires_at <= now)
        .all()
    )
    new_offers = []
    for entry in lapsed:
        entry.status = WaitlistStatus.EXPIRED
        entry.offer_token = None
        seat = entry.offered_seat
        # Only recycle the seat if it is still parked on this lapsed offer.
        if (seat is not None and seat.status == SeatStatus.HELD
                and seat.hold_kind == HoldKind.WAITLIST_OFFER
                and seat.held_by_id == entry.customer_id):
            nxt = offer_seat_to_next(db, seat)   # next in line (or available)
            if nxt is not None:
                new_offers.append(nxt)
    if lapsed:
        db.commit()
    return new_offers


def queue_position(db: Session, entry: WaitlistEntry) -> int:
    """1-based position among WAITING entries of the same event+category."""
    ahead = (
        db.query(WaitlistEntry)
        .filter(WaitlistEntry.event_id == entry.event_id,
                WaitlistEntry.category == entry.category,
                WaitlistEntry.status == WaitlistStatus.WAITING,
                WaitlistEntry.created_at <= entry.created_at,
                WaitlistEntry.id != entry.id)
        .count()
    )
    return ahead + 1
