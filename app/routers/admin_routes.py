"""
Admin endpoints: create/manage venues with a seat layout.

A venue layout is given as ordered blocks, e.g.
  [{category: "Premium", rows: 2, seats_per_row: 8},
   {category: "Standard", rows: 5, seats_per_row: 10}]
Rows are auto-labelled A, B, C... front to back across all blocks.
"""
import string

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import require_role
from ..database import get_db
from ..models import Role, Seat, User, Venue
from ..schemas import VenueCreate

router = APIRouter(prefix="/api", tags=["venues"])


@router.post("/admin/venues", status_code=201)
def create_venue(body: VenueCreate,
                 db: Session = Depends(get_db),
                 admin: User = Depends(require_role(Role.ADMIN))):
    total_rows = sum(b.rows for b in body.blocks)
    if total_rows > 26:
        raise HTTPException(400, "Layout too large: max 26 rows (A–Z)")

    venue = Venue(name=body.name, city=body.city, created_by_id=admin.id)
    db.add(venue)
    db.flush()  # need venue.id for the seats

    row_labels = iter(string.ascii_uppercase)
    for block in body.blocks:                    # front-to-back blocks
        for _ in range(block.rows):
            label = next(row_labels)
            for n in range(1, block.seats_per_row + 1):
                db.add(Seat(venue_id=venue.id, row_label=label,
                            seat_number=n, category=block.category))
    db.commit()
    return {"id": venue.id, "name": venue.name, "city": venue.city,
            "total_seats": sum(b.rows * b.seats_per_row for b in body.blocks)}


@router.get("/venues")
def list_venues(db: Session = Depends(get_db),
                _: User = Depends(require_role(Role.ORGANISER, Role.ADMIN))):
    """Organisers pick from this list when creating an event."""
    out = []
    for v in db.query(Venue).order_by(Venue.name).all():
        categories = sorted({s.category for s in v.seats})
        out.append({"id": v.id, "name": v.name, "city": v.city,
                    "total_seats": len(v.seats), "categories": categories})
    return out


@router.get("/venues/{venue_id}")
def venue_detail(venue_id: int, db: Session = Depends(get_db),
                 _: User = Depends(require_role(Role.ORGANISER, Role.ADMIN))):
    v = db.get(Venue, venue_id)
    if not v:
        raise HTTPException(404, "Venue not found")
    categories = sorted({s.category for s in v.seats})
    return {"id": v.id, "name": v.name, "city": v.city,
            "total_seats": len(v.seats), "categories": categories}
