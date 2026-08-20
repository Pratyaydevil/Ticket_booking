"""
Booking endpoints: confirm (from live holds), history, cancel (which feeds
the waitlist engine), QR ticket image, and gate-staff verification.
"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from sqlalchemy.orm import Session, joinedload

from ..auth import get_current_user, require_role
from ..database import get_db, utcnow
from ..models import (Booking, BookingStatus, Event, EventPrice, Role, User)
from ..schemas import BookingCreate
from ..services import email_service, qr_service, seat_service
from ..services.seat_service import SeatConflict

router = APIRouter(prefix="/api", tags=["bookings"])


def _booking_dict(b: Booking) -> dict:
    return {
        "id": b.id, "booking_ref": b.booking_ref, "status": b.status,
        "total_amount": b.total_amount, "created_at": b.created_at.isoformat(),
        "event": {"id": b.event.id, "title": b.event.title,
                  "event_type": b.event.event_type,
                  "starts_at": b.event.starts_at.isoformat(),
                  "venue": f"{b.event.venue.name}, {b.event.venue.city}"},
        "seats": [{"label": s.event_seat.seat.label,
                   "category": s.event_seat.seat.category,
                   "price": s.price} for s in b.seats],
    }


@router.post("/bookings", status_code=201)
def confirm_booking(body: BookingCreate, background: BackgroundTasks,
                    db: Session = Depends(get_db),
                    user: User = Depends(require_role(Role.CUSTOMER))):
    event = db.get(Event, body.event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    try:
        booking = seat_service.confirm_booking(db, user, event, body.seat_ids)
    except SeatConflict as conflict:
        raise HTTPException(409, {
            "message": "Your hold expired or a seat changed hands — "
                       "please reselect seats.",
            "unavailable_seat_ids": conflict.seat_ids,
        })
    # Email with QR ticket runs after the response (never blocks checkout).
    labels = [s.event_seat.seat.label for s in booking.seats]
    qr_png = qr_service.qr_png_bytes(booking.booking_ref)
    background.add_task(email_service.send_booking_confirmation,
                        booking, event, labels, qr_png)
    return _booking_dict(booking)


@router.get("/me/bookings")
def my_bookings(db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    bookings = (db.query(Booking)
                .options(joinedload(Booking.event).joinedload(Event.venue))
                .filter_by(customer_id=user.id)
                .order_by(Booking.created_at.desc()).all())
    return [_booking_dict(b) for b in bookings]


@router.post("/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: int, background: BackgroundTasks,
                   db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    booking = db.get(Booking, booking_id)
    if not booking or booking.customer_id != user.id:
        raise HTTPException(404, "Booking not found")
    if booking.status != BookingStatus.CONFIRMED:
        raise HTTPException(400, "Booking is already cancelled")
    if booking.event.starts_at <= utcnow():
        raise HTTPException(400, "Cannot cancel after the event has started")

    booking, offers = seat_service.cancel_booking(db, booking)

    # Notify: the canceller, and every waitlisted customer who got an offer.
    background.add_task(email_service.send_cancellation_note,
                        booking, booking.event)
    for entry in offers:
        price = (db.query(EventPrice)
                 .filter_by(event_id=entry.event_id,
                            category=entry.category).first())
        background.add_task(email_service.send_waitlist_offer, entry,
                            booking.event, entry.offered_seat.seat.label,
                            price.price if price else 0)
    return {"cancelled": True, "booking_ref": booking.booking_ref,
            "waitlist_offers_sent": len(offers)}


@router.get("/bookings/{booking_id}/qr")
def booking_qr(booking_id: int, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    """PNG QR for 'view ticket' in the UI (owner only)."""
    booking = db.get(Booking, booking_id)
    if not booking or booking.customer_id != user.id:
        raise HTTPException(404, "Booking not found")
    return Response(content=qr_service.qr_png_bytes(booking.booking_ref),
                    media_type="image/png")


@router.get("/verify/{booking_ref}")
def verify_ticket(booking_ref: str, db: Session = Depends(get_db),
                  _: User = Depends(require_role(Role.ORGANISER, Role.ADMIN))):
    """Gate check: scan the QR (it encodes booking_ref) and look it up."""
    booking = db.query(Booking).filter_by(booking_ref=booking_ref.upper()).first()
    if not booking:
        raise HTTPException(404, "Unknown booking reference")
    return {"valid": booking.status == BookingStatus.CONFIRMED,
            **_booking_dict(booking),
            "customer": booking.customer.name}
