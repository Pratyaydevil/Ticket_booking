"""
Event endpoints:
- public browse with filters + event detail with per-category availability
- the live seat map (per-seat status, expired holds shown as available)
- organiser: create event (materialises event_seats), list events, summary/revenue
"""
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from ..auth import get_current_user_optional, require_role
from ..config import SEAT_HOLD_TTL_SECONDS
from ..database import get_db, utcnow
from ..models import (Booking, BookingSeat, BookingStatus, Event, EventPrice,
                      EventSeat, Role, SeatStatus, User, Venue)
from ..schemas import EventCreate

router = APIRouter(prefix="/api", tags=["events"])


def _effective_status(seat: EventSeat, now: datetime) -> str:
    """Lazy TTL view: a hold past its expiry counts as available."""
    if (seat.status == SeatStatus.HELD and seat.hold_expires_at is not None
            and seat.hold_expires_at <= now):
        return SeatStatus.AVAILABLE
    return seat.status


def _category_stats(db: Session, event: Event, now: datetime):
    """Per category: price + live availability counts + sold_out flag."""
    prices = {p.category: p.price
              for p in db.query(EventPrice).filter_by(event_id=event.id)}
    counts = defaultdict(lambda: {"total": 0, "available": 0, "booked": 0})
    seats = (db.query(EventSeat).options(joinedload(EventSeat.seat))
             .filter_by(event_id=event.id).all())
    for es in seats:
        c = counts[es.seat.category]
        c["total"] += 1
        status = _effective_status(es, now)
        if status == SeatStatus.AVAILABLE:
            c["available"] += 1
        elif status == SeatStatus.BOOKED:
            c["booked"] += 1
    return [{"category": cat, "price": prices.get(cat, 0), **vals,
             "sold_out": vals["available"] == 0}
            for cat, vals in sorted(counts.items())]


def _event_card(db: Session, e: Event, now: datetime) -> dict:
    cats = _category_stats(db, e, now)
    return {
        "id": e.id, "title": e.title, "description": e.description,
        "event_type": e.event_type, "starts_at": e.starts_at.isoformat(),
        "venue": {"id": e.venue.id, "name": e.venue.name, "city": e.venue.city},
        "categories": cats,
        "sold_out": all(c["sold_out"] for c in cats) if cats else False,
        "min_price": min((c["price"] for c in cats), default=0),
    }


# ------------------------------ public browse --------------------------------

@router.get("/events")
def list_events(db: Session = Depends(get_db),
                event_type: Optional[str] = Query(None, alias="type"),
                date: Optional[str] = None,     # YYYY-MM-DD
                q: Optional[str] = None):
    now = utcnow()
    query = (db.query(Event).options(joinedload(Event.venue))
             .filter(Event.starts_at >= now)            # upcoming only
             .order_by(Event.starts_at))
    if event_type in ("movie", "concert"):
        query = query.filter(Event.event_type == event_type)
    if date:
        try:
            day = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "date must be YYYY-MM-DD")
        query = query.filter(Event.starts_at >= day,
                             Event.starts_at < day + timedelta(days=1))
    if q:
        query = query.filter(Event.title.ilike(f"%{q}%"))
    return [_event_card(db, e, now) for e in query.all()]


@router.get("/events/{event_id}")
def event_detail(event_id: int, db: Session = Depends(get_db)):
    e = db.get(Event, event_id)
    if not e:
        raise HTTPException(404, "Event not found")
    return _event_card(db, e, utcnow())


@router.get("/events/{event_id}/seatmap")
def seatmap(event_id: int, db: Session = Depends(get_db),
            user: Optional[User] = Depends(get_current_user_optional)):
    """
    The visual seat map. Polled by the frontend every few seconds so the
    grid reflects holds/releases/bookings in near real time.
    """
    e = db.get(Event, event_id)
    if not e:
        raise HTTPException(404, "Event not found")
    now = utcnow()
    seats = (db.query(EventSeat).options(joinedload(EventSeat.seat))
             .filter_by(event_id=event_id).all())

    rows = defaultdict(list)
    for es in sorted(seats, key=lambda s: (s.seat.row_label, s.seat.seat_number)):
        status = _effective_status(es, now)
        mine = (user is not None and status == SeatStatus.HELD
                and es.held_by_id == user.id)
        rows[es.seat.row_label].append({
            "id": es.id, "number": es.seat.seat_number,
            "label": es.seat.label, "category": es.seat.category,
            "status": status, "mine": mine,
            # expiry only exposed for the caller's own holds (privacy)
            "hold_expires_at": es.hold_expires_at.isoformat()
                               if mine and es.hold_expires_at else None,
        })
    return {
        "event": _event_card(db, e, now),
        "server_time": now.isoformat(),
        "hold_ttl_seconds": SEAT_HOLD_TTL_SECONDS,
        "rows": [{"row": r, "seats": s} for r, s in sorted(rows.items())],
    }


# ------------------------------ organiser ------------------------------------

@router.post("/events", status_code=201)
def create_event(body: EventCreate, db: Session = Depends(get_db),
                 organiser: User = Depends(require_role(Role.ORGANISER, Role.ADMIN))):
    venue = db.get(Venue, body.venue_id)
    if not venue:
        raise HTTPException(404, "Venue not found")
    venue_categories = {s.category for s in venue.seats}
    missing = venue_categories - set(body.prices)
    if missing:
        raise HTTPException(400, f"Missing price for categories: {sorted(missing)}")
    if any(p <= 0 for p in body.prices.values()):
        raise HTTPException(400, "Prices must be positive")

    event = Event(organiser_id=organiser.id, venue_id=venue.id,
                  title=body.title, description=body.description,
                  event_type=body.event_type,
                  starts_at=body.starts_at.replace(tzinfo=None))
    db.add(event)
    db.flush()
    for category in venue_categories:
        db.add(EventPrice(event_id=event.id, category=category,
                          price=body.prices[category]))
    # Materialise one status row per physical seat for this show.
    for seat in venue.seats:
        db.add(EventSeat(event_id=event.id, seat_id=seat.id,
                         status=SeatStatus.AVAILABLE))
    db.commit()
    return {"id": event.id, "title": event.title,
            "seats_created": len(venue.seats)}


@router.get("/organiser/events")
def my_events(db: Session = Depends(get_db),
              organiser: User = Depends(require_role(Role.ORGANISER, Role.ADMIN))):
    now = utcnow()
    events = (db.query(Event).options(joinedload(Event.venue))
              .filter_by(organiser_id=organiser.id)
              .order_by(Event.starts_at.desc()).all())
    return [_event_card(db, e, now) for e in events]


@router.get("/organiser/events/{event_id}/summary")
def event_summary(event_id: int, db: Session = Depends(get_db),
                  organiser: User = Depends(require_role(Role.ORGANISER, Role.ADMIN))):
    """Booking summary + revenue for one of my events (spec requirement)."""
    e = db.get(Event, event_id)
    if not e or e.organiser_id != organiser.id:
        raise HTTPException(404, "Event not found")
    now = utcnow()

    confirmed = (db.query(Booking)
                 .filter_by(event_id=event_id, status=BookingStatus.CONFIRMED)
                 .all())
    revenue = sum(b.total_amount for b in confirmed)
    tickets_sold = (db.query(BookingSeat).join(Booking)
                    .filter(Booking.event_id == event_id,
                            Booking.status == BookingStatus.CONFIRMED)
                    .count())
    cancelled = (db.query(Booking)
                 .filter_by(event_id=event_id, status=BookingStatus.CANCELLED)
                 .count())
    return {
        "event": _event_card(db, e, now),
        "confirmed_bookings": len(confirmed),
        "cancelled_bookings": cancelled,
        "tickets_sold": tickets_sold,
        "revenue": revenue,   # confirmed bookings only; cancellations refunded
        "by_category": _category_stats(db, e, now),
    }
