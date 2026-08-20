"""
Background sweeper — the "scheduler" half of TTL enforcement.

Runs inside the FastAPI process (started from the app lifespan) every
SWEEP_INTERVAL_SECONDS and does two jobs:

1. release_expired_cart_holds(): abandoned-checkout holds -> available,
   so the seat map frees up in near real time.
2. expire_lapsed_offers(): waitlist offers past their deadline -> EXPIRED,
   seat cascades to the next customer in line (who then gets the offer email).

Lazy expiry checks in seat_service make the system correct even if the
sweeper were down; the sweeper makes it *live*.
"""
import asyncio

from ..config import SWEEP_INTERVAL_SECONDS, WAITLIST_OFFER_TTL_SECONDS
from ..database import SessionLocal
from ..models import EventPrice
from . import email_service, seat_service, waitlist_service


def sweep_once(db) -> int:
    """One pass. Returns number of cart holds released (handy for tests)."""
    released = seat_service.release_expired_cart_holds(db)
    fresh_offers = waitlist_service.expire_lapsed_offers(db)
    for entry in fresh_offers:                      # notify the next in line
        seat = entry.offered_seat
        price = (db.query(EventPrice)
                   .filter_by(event_id=entry.event_id,
                              category=entry.category).first())
        email_service.send_waitlist_offer(
            entry, entry.event, seat.seat.label,
            price.price if price else 0)
    return released


async def sweeper_loop():
    """Forever loop; each pass gets a fresh short-lived DB session."""
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            with SessionLocal() as db:
                sweep_once(db)
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            print(f"[sweeper] pass failed: {exc}")
