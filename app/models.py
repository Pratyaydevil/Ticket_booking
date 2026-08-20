"""
Database models.

Key modelling decision (evaluation focus — "seat map data model"):
a venue owns physical Seats; when an Event is created we materialise one
EventSeat row per physical seat. EventSeat carries the per-show status
(available / held / booked) plus hold ownership + expiry. All concurrency
control happens on this one table via atomic conditional UPDATEs.
"""
from datetime import datetime

from sqlalchemy import (Column, DateTime, ForeignKey, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import relationship

from .database import Base, utcnow


# --- enum-like string constants (kept as strings for SQLite portability) ----
class Role:
    CUSTOMER = "customer"
    ORGANISER = "organiser"
    ADMIN = "admin"


class SeatStatus:
    AVAILABLE = "available"
    HELD = "held"
    BOOKED = "booked"


class HoldKind:
    CART = "cart"                     # normal checkout hold (10-min TTL)
    WAITLIST_OFFER = "waitlist_offer"  # seat reserved for a waitlisted customer


class BookingStatus:
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class WaitlistStatus:
    WAITING = "waiting"      # in queue
    OFFERED = "offered"      # has a live time-limited offer
    EXPIRED = "expired"      # offer timed out -> seat moved to next in line
    CONVERTED = "converted"  # completed the booking from the offer
    CANCELLED = "cancelled"  # left the queue voluntarily


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default=Role.CUSTOMER)
    created_at = Column(DateTime, default=utcnow)


class Venue(Base):
    __tablename__ = "venues"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    city = Column(String(80), nullable=False)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=utcnow)

    seats = relationship("Seat", back_populates="venue",
                         cascade="all, delete-orphan",
                         order_by="Seat.row_label, Seat.seat_number")


class Seat(Base):
    """A physical seat in a venue, e.g. row 'A', number 3, category 'Premium'."""
    __tablename__ = "seats"
    __table_args__ = (UniqueConstraint("venue_id", "row_label", "seat_number"),)
    id = Column(Integer, primary_key=True)
    venue_id = Column(Integer, ForeignKey("venues.id"), nullable=False, index=True)
    row_label = Column(String(5), nullable=False)
    seat_number = Column(Integer, nullable=False)
    category = Column(String(50), nullable=False)  # e.g. Premium / Standard

    venue = relationship("Venue", back_populates="seats")

    @property
    def label(self) -> str:
        return f"{self.row_label}{self.seat_number}"


class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True)
    organiser_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    venue_id = Column(Integer, ForeignKey("venues.id"), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text, default="")
    event_type = Column(String(20), nullable=False)  # movie | concert
    starts_at = Column(DateTime, nullable=False)     # local venue time
    created_at = Column(DateTime, default=utcnow)

    venue = relationship("Venue")
    prices = relationship("EventPrice", cascade="all, delete-orphan")


class EventPrice(Base):
    """Per-category ticket price for one event (amount in whole INR)."""
    __tablename__ = "event_prices"
    __table_args__ = (UniqueConstraint("event_id", "category"),)
    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    category = Column(String(50), nullable=False)
    price = Column(Integer, nullable=False)


class EventSeat(Base):
    """
    Per-show status of one physical seat — the single source of truth that
    every hold/booking races on. UNIQUE(event_id, seat_id) guarantees exactly
    one status row per seat per show.
    """
    __tablename__ = "event_seats"
    __table_args__ = (UniqueConstraint("event_id", "seat_id"),)
    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    seat_id = Column(Integer, ForeignKey("seats.id"), nullable=False)
    status = Column(String(20), nullable=False, default=SeatStatus.AVAILABLE, index=True)
    held_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    hold_kind = Column(String(20), nullable=True)        # cart | waitlist_offer
    hold_expires_at = Column(DateTime, nullable=True)    # when the hold lapses

    seat = relationship("Seat")


class Booking(Base):
    __tablename__ = "bookings"
    id = Column(Integer, primary_key=True)
    booking_ref = Column(String(20), unique=True, nullable=False, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    customer_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    status = Column(String(20), nullable=False, default=BookingStatus.CONFIRMED)
    total_amount = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=utcnow)
    cancelled_at = Column(DateTime, nullable=True)

    event = relationship("Event")
    customer = relationship("User")
    seats = relationship("BookingSeat", cascade="all, delete-orphan")


class BookingSeat(Base):
    """Which seats a booking covers + price paid (kept for history/revenue)."""
    __tablename__ = "booking_seats"
    id = Column(Integer, primary_key=True)
    booking_id = Column(Integer, ForeignKey("bookings.id"), nullable=False, index=True)
    event_seat_id = Column(Integer, ForeignKey("event_seats.id"), nullable=False)
    price = Column(Integer, nullable=False)

    event_seat = relationship("EventSeat")


class WaitlistEntry(Base):
    """
    FIFO queue per (event, seat category). Ordering = created_at.
    When a booked seat frees up it is offered to the oldest WAITING entry:
    the seat is re-held for that customer and a time-limited token link is
    emailed. Expiry moves the seat to the next entry automatically.
    """
    __tablename__ = "waitlist_entries"
    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    category = Column(String(50), nullable=False)
    customer_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    status = Column(String(20), nullable=False, default=WaitlistStatus.WAITING, index=True)
    created_at = Column(DateTime, default=utcnow)
    offer_token = Column(String(64), unique=True, nullable=True)
    offer_expires_at = Column(DateTime, nullable=True)
    offered_event_seat_id = Column(Integer, ForeignKey("event_seats.id"), nullable=True)

    event = relationship("Event")
    customer = relationship("User")
    offered_seat = relationship("EventSeat")
