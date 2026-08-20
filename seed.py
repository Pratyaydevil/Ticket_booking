"""
Idempotent seed: demo accounts, two venues, three events — including a tiny
SOLD-OUT show so the waitlist flow can be demoed in under a minute.

Run:  python seed.py
Safe to re-run (skips if the admin user already exists).

Demo logins (password for all: Demo@1234)
  admin@ticketbox.dev       admin      creates venues
  organiser@ticketbox.dev   organiser  creates events, views revenue
  asha@ticketbox.dev        customer   has one confirmed booking
  ravi@ticketbox.dev        customer   holds the sold-out show's other seats
  meera@ticketbox.dev       customer   free account — use to join waitlists
"""
from datetime import timedelta

from app.auth import hash_password
from app.database import Base, SessionLocal, engine, utcnow
from app.models import (Booking, BookingSeat, BookingStatus, Event,
                        EventPrice, EventSeat, Role, Seat, SeatStatus, User,
                        Venue)

PASSWORD = "Demo@1234"


def _user(db, name, email, role):
    u = User(name=name, email=email, role=role,
             password_hash=hash_password(PASSWORD))
    db.add(u)
    return u


def _venue(db, admin, name, city, blocks):
    """blocks: list of (category, rows, seats_per_row), front to back."""
    import string
    v = Venue(name=name, city=city, created_by_id=admin.id)
    db.add(v)
    db.flush()
    labels = iter(string.ascii_uppercase)
    for category, rows, per_row in blocks:
        for _ in range(rows):
            row = next(labels)
            for n in range(1, per_row + 1):
                db.add(Seat(venue_id=v.id, row_label=row, seat_number=n,
                            category=category))
    db.flush()
    return v


def _event(db, organiser, venue, title, etype, starts_at, prices, desc=""):
    e = Event(organiser_id=organiser.id, venue_id=venue.id, title=title,
              description=desc, event_type=etype, starts_at=starts_at)
    db.add(e)
    db.flush()
    for cat, price in prices.items():
        db.add(EventPrice(event_id=e.id, category=cat, price=price))
    for seat in venue.seats:
        db.add(EventSeat(event_id=e.id, seat_id=seat.id,
                         status=SeatStatus.AVAILABLE))
    db.flush()
    return e


def _book(db, customer, event, seat_labels):
    """Directly mark seats booked + create the booking row (seed only)."""
    import secrets
    seats = [es for es in db.query(EventSeat).filter_by(event_id=event.id)
             if es.seat.label in seat_labels]
    prices = {p.category: p.price
              for p in db.query(EventPrice).filter_by(event_id=event.id)}
    total = sum(prices[s.seat.category] for s in seats)
    b = Booking(booking_ref="TBS-" + secrets.token_hex(4).upper(),
                event_id=event.id, customer_id=customer.id,
                status=BookingStatus.CONFIRMED, total_amount=total)
    db.add(b)
    db.flush()
    for s in seats:
        s.status = SeatStatus.BOOKED
        db.add(BookingSeat(booking_id=b.id, event_seat_id=s.id,
                           price=prices[s.seat.category]))
    return b


def run():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(User).filter_by(role=Role.ADMIN).first():
            print("Seed: admin already exists — skipping (idempotent).")
            return

        admin = _user(db, "Site Admin", "admin@ticketbox.dev", Role.ADMIN)
        org = _user(db, "Nova Live Events", "organiser@ticketbox.dev",
                    Role.ORGANISER)
        asha = _user(db, "Asha Iyer", "asha@ticketbox.dev", Role.CUSTOMER)
        ravi = _user(db, "Ravi Menon", "ravi@ticketbox.dev", Role.CUSTOMER)
        _user(db, "Meera Pillai", "meera@ticketbox.dev", Role.CUSTOMER)
        db.flush()

        grand = _venue(db, admin, "Grand Orion Cinema", "Bengaluru",
                       [("Premium", 2, 8), ("Standard", 5, 10)])   # 66 seats
        blackbox = _venue(db, admin, "Black Box Studio", "Bengaluru",
                          [("Premium", 1, 4)])                     # 4 seats

        now = utcnow()
        movie = _event(db, org, grand, "Interstellar — IMAX Re-release",
                       "movie", now + timedelta(days=2, hours=3),
                       {"Premium": 450, "Standard": 250},
                       "Nolan's space epic back on the big screen.")
        _event(db, org, grand, "Indie Waves: Live in Concert", "concert",
               now + timedelta(days=7),
               {"Premium": 1500, "Standard": 900},
               "An evening of independent music under one roof.")
        soldout = _event(db, org, blackbox,
                         "Midnight Jazz Session (tiny hall)", "concert",
                         now + timedelta(days=3),
                         {"Premium": 800},
                         "Intimate 4-seat studio session — always sells out.")

        # A little life in the data: one normal booking + a fully sold-out show.
        _book(db, asha, movie, ["A1", "A2"])
        _book(db, asha, soldout, ["A1", "A2"])
        _book(db, ravi, soldout, ["A3", "A4"])

        db.commit()
        print("Seed complete.")
        print(f"  Venues : {grand.name} (66 seats), {blackbox.name} (4 seats)")
        print("  Events : 2 open + 1 SOLD OUT (for the waitlist demo)")
        print(f"  Logins : see docstring — password for all: {PASSWORD}")
        print("\nWaitlist demo: log in as meera@ticketbox.dev, open the "
              "sold-out Midnight Jazz Session, join the waitlist; then log in "
              "as asha@ticketbox.dev and cancel her jazz booking — Meera "
              "gets a time-limited offer email (see ./outbox).")
    finally:
        db.close()


if __name__ == "__main__":
    run()
