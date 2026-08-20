"""
Seat lifecycle: hold -> (expire | book) -> (cancel -> waitlist offer).

CONCURRENCY MODEL (evaluation focus)
------------------------------------
Every state change is an *atomic conditional UPDATE* — a compare-and-set on
the event_seats row:

    UPDATE event_seats
       SET status='held', held_by=:me, hold_expires_at=:exp
     WHERE id=:seat AND (status='available'
                         OR (status='held' AND hold_expires_at <= :now))

The database applies row locks while evaluating the WHERE clause, so if two
customers race for the same seat exactly one UPDATE matches (rowcount=1) and
the other matches nothing (rowcount=0) — it can never double-assign.
* SQLite: writers are fully serialised, so the check+write is atomic.
* PostgreSQL: the second UPDATE blocks on the row lock, then re-evaluates the
  WHERE clause against the committed row and matches 0 rows.
If any seat in a multi-seat request fails, the whole transaction is rolled
back — holds and bookings are all-or-nothing.

TTL MODEL
---------
Expiry is enforced twice, so correctness never depends on a timer firing:
1. Lazily: every conditional UPDATE and every seat-map read treats a hold
   whose hold_expires_at <= now as available.
2. Actively: a background sweeper (services/sweeper.py) flips expired holds
   back to 'available' every few seconds so other customers see seats free
   in near real time, and rotates expired waitlist offers to the next person.
"""
import secrets
from datetime import timedelta
from typing import List, Optional, Tuple

from sqlalchemy import and_, or_, update
from sqlalchemy.orm import Session

from ..config import SEAT_HOLD_TTL_SECONDS
from ..database import utcnow
from ..models import (Booking, BookingSeat, BookingStatus, Event, EventPrice,
                      EventSeat, HoldKind, SeatStatus, User, WaitlistEntry)
from . import waitlist_service


class SeatConflict(Exception):
    """Raised when one of the requested seats was taken by someone else."""
    def __init__(self, seat_ids: List[int]):
        self.seat_ids = seat_ids
        super().__init__(f"Seats no longer available: {seat_ids}")


def _grabbable(now):
    """WHERE fragment: seat is free, or its previous hold has lapsed (lazy TTL)."""
    return or_(
        EventSeat.status == SeatStatus.AVAILABLE,
        and_(EventSeat.status == SeatStatus.HELD,
             EventSeat.hold_expires_at <= now),
    )


def acquire_holds(db: Session, user: User, event_id: int,
                  seat_ids: List[int]):
    """
    Place a TTL hold on each requested seat for `user`.
    All-or-nothing: any single failure rolls back every seat in the request.
    Returns the hold expiry datetime.
    """
    now = utcnow()
    expires = now + timedelta(seconds=SEAT_HOLD_TTL_SECONDS)
    failed: List[int] = []

    for seat_id in seat_ids:
        result = db.execute(
            update(EventSeat)
            .where(
                EventSeat.id == seat_id,
                EventSeat.event_id == event_id,
                _grabbable(now),                      # atomic compare-and-set
            )
            .values(status=SeatStatus.HELD, held_by_id=user.id,
                    hold_kind=HoldKind.CART, hold_expires_at=expires)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:      # someone else holds/booked this seat
            failed.append(seat_id)

    if failed:
        db.rollback()                 # release any seats we grabbed this call
        raise SeatConflict(failed)

    db.commit()
    return expires


def release_my_cart_holds(db: Session, user: User, event_id: int) -> int:
    """Explicit release when the customer leaves/cancels checkout."""
    result = db.execute(
        update(EventSeat)
        .where(EventSeat.event_id == event_id,
               EventSeat.status == SeatStatus.HELD,
               EventSeat.held_by_id == user.id,
               EventSeat.hold_kind == HoldKind.CART)
        .values(status=SeatStatus.AVAILABLE, held_by_id=None,
                hold_kind=None, hold_expires_at=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount


def _new_booking_ref(db: Session) -> str:
    """Short human-friendly unique reference, e.g. TBS-9F3A21C4."""
    while True:
        ref = "TBS-" + secrets.token_hex(4).upper()
        if not db.query(Booking.id).filter_by(booking_ref=ref).first():
            return ref


def confirm_booking(db: Session, user: User, event: Event,
                    seat_ids: List[int],
                    waitlist_entry: Optional[WaitlistEntry] = None) -> Booking:
    """
    Convert the caller's live holds into a confirmed booking.
    Same compare-and-set pattern: each seat must still be HELD *by this user*
    and not expired, otherwise the whole booking rolls back.
    """
    now = utcnow()
    failed: List[int] = []
    for seat_id in seat_ids:
        result = db.execute(
            update(EventSeat)
            .where(EventSeat.id == seat_id,
                   EventSeat.event_id == event.id,
                   EventSeat.status == SeatStatus.HELD,
                   EventSeat.held_by_id == user.id,      # must be MY hold
                   EventSeat.hold_expires_at > now)      # and still alive
            .values(status=SeatStatus.BOOKED, held_by_id=None,
                    hold_kind=None, hold_expires_at=None)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            failed.append(seat_id)
    if failed:
        db.rollback()
        raise SeatConflict(failed)

    # Price each seat from the event's per-category price list.
    price_map = {p.category: p.price
                 for p in db.query(EventPrice).filter_by(event_id=event.id)}
    seats = db.query(EventSeat).filter(EventSeat.id.in_(seat_ids)).all()
    total = sum(price_map[s.seat.category] for s in seats)

    booking = Booking(booking_ref=_new_booking_ref(db), event_id=event.id,
                      customer_id=user.id, status=BookingStatus.CONFIRMED,
                      total_amount=total)
    db.add(booking)
    db.flush()  # get booking.id before adding seat lines
    for s in seats:
        db.add(BookingSeat(booking_id=booking.id, event_seat_id=s.id,
                           price=price_map[s.seat.category]))

    if waitlist_entry is not None:   # booking came from a waitlist offer
        waitlist_entry.status = "converted"
        waitlist_entry.offer_token = None

    db.commit()
    db.refresh(booking)
    return booking


def cancel_booking(db: Session, booking: Booking) -> Tuple[Booking, list]:
    """
    Cancel a confirmed booking and free its seats.
    Each freed seat is immediately routed through the waitlist engine:
    offered to the next WAITING customer in that seat's category, or made
    available if the queue is empty. Returns (booking, offers_to_email).
    """
    booking.status = BookingStatus.CANCELLED
    booking.cancelled_at = utcnow()

    offers = []   # WaitlistEntry rows that got a fresh offer -> email after commit
    for line in booking.seats:
        seat = line.event_seat
        if seat.status != SeatStatus.BOOKED:
            continue  # defensive: seat already recycled somehow
        entry = waitlist_service.offer_seat_to_next(db, seat)
        if entry is not None:
            offers.append(entry)

    db.commit()
    return booking, offers


def release_expired_cart_holds(db: Session) -> int:
    """Sweeper job 1: flip lapsed checkout holds back to available."""
    now = utcnow()
    result = db.execute(
        update(EventSeat)
        .where(EventSeat.status == SeatStatus.HELD,
               EventSeat.hold_kind == HoldKind.CART,
               EventSeat.hold_expires_at <= now)
        .values(status=SeatStatus.AVAILABLE, held_by_id=None,
                hold_kind=None, hold_expires_at=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount
