"""
Waitlist endpoints: join (only when the category is sold out), view my
entries with live queue position, leave, and the time-limited offer flow
(view offer by token + confirm booking from the offer).
"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import require_role
from ..database import get_db, utcnow
from ..models import (Event, EventPrice, Role, SeatStatus, User,
                      WaitlistEntry, WaitlistStatus)
from ..schemas import WaitlistJoin
from ..services import email_service, qr_service, seat_service, waitlist_service
from ..services.seat_service import SeatConflict
from .event_routes import _category_stats

router = APIRouter(prefix="/api", tags=["waitlist"])

ACTIVE = (WaitlistStatus.WAITING, WaitlistStatus.OFFERED)


@router.post("/events/{event_id}/waitlist", status_code=201)
def join_waitlist(event_id: int, body: WaitlistJoin,
                  db: Session = Depends(get_db),
                  user: User = Depends(require_role(Role.CUSTOMER))):
    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(404, "Event not found")

    stats = {c["category"]: c for c in _category_stats(db, event, utcnow())}
    if body.category not in stats:
        raise HTTPException(400, "Unknown seat category for this event")
    if not stats[body.category]["sold_out"]:
        raise HTTPException(400, "Seats are still available in this category "
                                 "— book directly from the seat map")
    dup = (db.query(WaitlistEntry)
           .filter(WaitlistEntry.event_id == event_id,
                   WaitlistEntry.category == body.category,
                   WaitlistEntry.customer_id == user.id,
                   WaitlistEntry.status.in_(ACTIVE)).first())
    if dup:
        raise HTTPException(400, "You are already on this waitlist")

    entry = WaitlistEntry(event_id=event_id, category=body.category,
                          customer_id=user.id)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return {"entry_id": entry.id,
            "position": waitlist_service.queue_position(db, entry)}


@router.get("/me/waitlist")
def my_waitlist(db: Session = Depends(get_db),
                user: User = Depends(require_role(Role.CUSTOMER))):
    entries = (db.query(WaitlistEntry).filter_by(customer_id=user.id)
               .order_by(WaitlistEntry.created_at.desc()).all())
    out = []
    for e in entries:
        item = {"entry_id": e.id, "category": e.category, "status": e.status,
                "event": {"id": e.event.id, "title": e.event.title,
                          "starts_at": e.event.starts_at.isoformat()}}
        if e.status == WaitlistStatus.WAITING:
            item["position"] = waitlist_service.queue_position(db, e)
        if e.status == WaitlistStatus.OFFERED:
            item["offer_token"] = e.offer_token
            item["offer_expires_at"] = e.offer_expires_at.isoformat()
        out.append(item)
    return out


@router.delete("/waitlist/{entry_id}")
def leave_waitlist(entry_id: int, background: BackgroundTasks,
                   db: Session = Depends(get_db),
                   user: User = Depends(require_role(Role.CUSTOMER))):
    entry = db.get(WaitlistEntry, entry_id)
    if not entry or entry.customer_id != user.id:
        raise HTTPException(404, "Waitlist entry not found")
    if entry.status not in ACTIVE:
        raise HTTPException(400, "This entry is no longer active")

    # If they had a live offer, recycle the reserved seat to the next in line.
    if entry.status == WaitlistStatus.OFFERED and entry.offered_seat is not None:
        seat = entry.offered_seat
        if (seat.status == SeatStatus.HELD
                and seat.held_by_id == user.id):
            nxt = waitlist_service.offer_seat_to_next(db, seat)
            if nxt is not None:
                price = (db.query(EventPrice)
                         .filter_by(event_id=nxt.event_id,
                                    category=nxt.category).first())
                background.add_task(email_service.send_waitlist_offer, nxt,
                                    nxt.event, seat.seat.label,
                                    price.price if price else 0)
    entry.status = WaitlistStatus.CANCELLED
    entry.offer_token = None
    db.commit()
    return {"left": True}


# ------------------------- time-limited offer flow ---------------------------

def _live_offer_or_410(db: Session, token: str) -> WaitlistEntry:
    entry = db.query(WaitlistEntry).filter_by(offer_token=token).first()
    if not entry or entry.status != WaitlistStatus.OFFERED:
        raise HTTPException(404, "Offer not found")
    if entry.offer_expires_at <= utcnow():
        # Lazy expiry: don't wait for the sweeper — rotate right now.
        for nxt in waitlist_service.expire_lapsed_offers(db):
            price = (db.query(EventPrice)
                     .filter_by(event_id=nxt.event_id,
                                category=nxt.category).first())
            email_service.send_waitlist_offer(nxt, nxt.event,
                                              nxt.offered_seat.seat.label,
                                              price.price if price else 0)
        raise HTTPException(410, "This offer has expired — the seat was "
                                 "offered to the next person in line")
    return entry


@router.get("/waitlist/offer/{token}")
def view_offer(token: str, db: Session = Depends(get_db),
               user: User = Depends(require_role(Role.CUSTOMER))):
    entry = _live_offer_or_410(db, token)
    if entry.customer_id != user.id:
        raise HTTPException(403, "This offer belongs to a different account")
    seat = entry.offered_seat
    price = (db.query(EventPrice)
             .filter_by(event_id=entry.event_id, category=entry.category)
             .first())
    return {
        "event": {"id": entry.event.id, "title": entry.event.title,
                  "starts_at": entry.event.starts_at.isoformat(),
                  "venue": f"{entry.event.venue.name}, {entry.event.venue.city}"},
        "seat": {"id": seat.id, "label": seat.seat.label,
                 "category": entry.category},
        "price": price.price if price else 0,
        "offer_expires_at": entry.offer_expires_at.isoformat(),
        "server_time": utcnow().isoformat(),
    }


@router.post("/waitlist/offer/{token}/confirm", status_code=201)
def confirm_offer(token: str, background: BackgroundTasks,
                  db: Session = Depends(get_db),
                  user: User = Depends(require_role(Role.CUSTOMER))):
    entry = _live_offer_or_410(db, token)
    if entry.customer_id != user.id:
        raise HTTPException(403, "This offer belongs to a different account")
    try:
        booking = seat_service.confirm_booking(
            db, user, entry.event, [entry.offered_event_seat_id],
            waitlist_entry=entry)
    except SeatConflict:
        raise HTTPException(409, "The reserved seat is no longer held for "
                                 "you — the offer may have just expired.")
    labels = [s.event_seat.seat.label for s in booking.seats]
    qr_png = qr_service.qr_png_bytes(booking.booking_ref)
    background.add_task(email_service.send_booking_confirmation,
                        booking, entry.event, labels, qr_png)
    return {"booking_ref": booking.booking_ref, "booking_id": booking.id,
            "total_amount": booking.total_amount}
