"""
Pydantic request bodies (input validation). Responses are plain dicts built
in the routers so the JSON shape is explicit and easy to read in one place.
"""
from datetime import datetime
from typing import Dict, List, Literal

from pydantic import BaseModel, EmailStr, Field


# --- auth --------------------------------------------------------------------
class RegisterIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    # public self-registration is allowed for customers and organisers;
    # the admin account is created by the seed script only.
    role: Literal["customer", "organiser"] = "customer"


class LoginIn(BaseModel):
    email: EmailStr
    password: str


# --- admin: venues -------------------------------------------------------------
class SeatBlock(BaseModel):
    """A block of identical rows, e.g. 2 Premium rows with 8 seats each."""
    category: str = Field(min_length=2, max_length=50)
    rows: int = Field(ge=1, le=26)
    seats_per_row: int = Field(ge=1, le=40)


class VenueCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    city: str = Field(min_length=2, max_length=80)
    blocks: List[SeatBlock] = Field(min_length=1)


# --- organiser: events ---------------------------------------------------------
class EventCreate(BaseModel):
    venue_id: int
    title: str = Field(min_length=2, max_length=200)
    description: str = ""
    event_type: Literal["movie", "concert"]
    starts_at: datetime                     # local venue time
    prices: Dict[str, int]                  # {"Premium": 450, "Standard": 250}


# --- customer: holds / bookings / waitlist -------------------------------------
class HoldCreate(BaseModel):
    seat_ids: List[int] = Field(min_length=1, max_length=10)  # event_seat ids


class BookingCreate(BaseModel):
    event_id: int
    seat_ids: List[int] = Field(min_length=1, max_length=10)  # my held seats


class WaitlistJoin(BaseModel):
    category: str
