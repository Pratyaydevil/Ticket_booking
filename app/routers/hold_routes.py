"""
Seat hold endpoints (checkout step 1).
POST places an all-or-nothing TTL hold; DELETE releases it early when the
customer backs out. Abandoned holds expire on their own via the sweeper.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import require_role
from ..database import get_db
from ..models import Event, Role, User
from ..schemas import HoldCreate
from ..services import seat_service
from ..services.seat_service import SeatConflict

router = APIRouter(prefix="/api/events/{event_id}/holds", tags=["holds"])


@router.post("", status_code=201)
def place_hold(event_id: int, body: HoldCreate,
               db: Session = Depends(get_db),
               user: User = Depends(require_role(Role.CUSTOMER))):
    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    try:
        expires = seat_service.acquire_holds(db, user, event_id, body.seat_ids)
    except SeatConflict as conflict:
        # 409: another customer holds/booked at least one requested seat.
        raise HTTPException(409, {
            "message": "Some seats were just taken — pick different seats.",
            "unavailable_seat_ids": conflict.seat_ids,
        })
    return {"held_seat_ids": body.seat_ids,
            "hold_expires_at": expires.isoformat()}


@router.delete("")
def release_hold(event_id: int, db: Session = Depends(get_db),
                 user: User = Depends(require_role(Role.CUSTOMER))):
    released = seat_service.release_my_cart_holds(db, user, event_id)
    return {"released": released}
