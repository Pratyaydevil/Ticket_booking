"""
End-to-end smoke test for every evaluation focus area.

Run:  python tests/smoke_test.py
Uses FastAPI's TestClient against a throwaway SQLite DB with 1-second TTLs,
so hold expiry and waitlist-offer rotation are tested for real, fast.

Covers:
 1. Auth + roles (admin venue, organiser event, customer booking)
 2. Seat hold + all-or-nothing concurrency (two customers race one seat)
 3. Hold TTL auto-release (expired hold becomes grabbable again)
 4. Booking confirm + QR endpoint + verify endpoint
 5. Cancellation -> waitlist auto-offer (time-limited, correct customer)
 6. Offer expiry -> seat cascades to the NEXT person in line
 7. Offer claim -> booking converted
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# --- test config BEFORE importing the app ------------------------------------
# Throwaway DB in the system temp dir (keeps the repo clean; override with
# TEST_DB_PATH if you want it elsewhere).
import tempfile                                     # noqa: E402
TEST_DB = Path(os.environ.get("TEST_DB_PATH",
                              Path(tempfile.gettempdir()) / "tbs_smoke.db"))
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB}"
os.environ["SEAT_HOLD_TTL_SECONDS"] = "1"        # tiny TTLs so the test is fast
os.environ["WAITLIST_OFFER_TTL_SECONDS"] = "1"
os.environ["EMAIL_MODE"] = "console"
os.environ["OUTBOX_DIR"] = "./test_outbox"

from fastapi.testclient import TestClient          # noqa: E402

from app.database import SessionLocal              # noqa: E402
from app.main import app                           # noqa: E402
from app.services.sweeper import sweep_once        # noqa: E402

PASSED = 0


def ok(condition, label):
    global PASSED
    assert condition, f"FAILED: {label}"
    PASSED += 1
    print(f"  ✓ {label}")


def sweep():
    """Run one sweeper pass deterministically (instead of waiting for the loop)."""
    with SessionLocal() as db:
        sweep_once(db)


with TestClient(app) as client:

    def register(name, email, role="customer"):
        r = client.post("/api/auth/register", json={
            "name": name, "email": email, "password": "Passw0rd!", "role": role})
        assert r.status_code == 201, r.text
        return {"Authorization": "Bearer " + r.json()["token"]}

    print("\n[1] Auth & roles")
    organiser = register("Org", "org@test.dev", "organiser")
    cust1 = register("Customer One", "c1@test.dev")
    cust2 = register("Customer Two", "c2@test.dev")
    cust3 = register("Customer Three", "c3@test.dev")

    # Admin is seed-only: create directly, then log in through the API.
    from app.auth import hash_password
    from app.models import Role, User
    with SessionLocal() as db:
        db.add(User(name="Admin", email="admin@test.dev",
                    password_hash=hash_password("Passw0rd!"), role=Role.ADMIN))
        db.commit()
    r = client.post("/api/auth/login",
                    json={"email": "admin@test.dev", "password": "Passw0rd!"})
    admin = {"Authorization": "Bearer " + r.json()["token"]}
    ok(r.status_code == 200, "admin login")
    r = client.post("/api/admin/venues", headers=cust1, json={
        "name": "X", "city": "Y",
        "blocks": [{"category": "Standard", "rows": 1, "seats_per_row": 2}]})
    ok(r.status_code == 403, "customer blocked from admin endpoint (403)")

    print("\n[2] Venue + event setup")
    r = client.post("/api/admin/venues", headers=admin, json={
        "name": "Test Hall", "city": "Bengaluru",
        "blocks": [{"category": "Premium", "rows": 1, "seats_per_row": 2},
                   {"category": "Standard", "rows": 1, "seats_per_row": 2}]})
    ok(r.status_code == 201 and r.json()["total_seats"] == 4, "admin creates venue (4 seats)")
    venue_id = r.json()["id"]

    r = client.post("/api/events", headers=organiser, json={
        "venue_id": venue_id, "title": "Smoke Show", "event_type": "concert",
        "starts_at": "2030-01-01T20:00:00",
        "prices": {"Premium": 500, "Standard": 200}})
    ok(r.status_code == 201 and r.json()["seats_created"] == 4,
       "organiser creates event; event_seats materialised")
    event_id = r.json()["id"]

    r = client.get(f"/api/events/{event_id}/seatmap")
    seatmap = r.json()
    all_ids = [s["id"] for row in seatmap["rows"] for s in row["seats"]]
    ok(len(all_ids) == 4 and all(
        s["status"] == "available" for row in seatmap["rows"] for s in row["seats"]),
       "seat map shows 4 available seats")
    target = all_ids[0]

    print("\n[3] Concurrency: two customers race the same seat")
    r1 = client.post(f"/api/events/{event_id}/holds", headers=cust1,
                     json={"seat_ids": [target]})
    ok(r1.status_code == 201, "customer 1 holds the seat")
    r2 = client.post(f"/api/events/{event_id}/holds", headers=cust2,
                     json={"seat_ids": [target]})
    ok(r2.status_code == 409 and target in
       r2.json()["detail"]["unavailable_seat_ids"],
       "customer 2 gets 409 on the same seat (no double hold)")
    # All-or-nothing: c2 asks for [held seat + free seat] -> neither is granted.
    r3 = client.post(f"/api/events/{event_id}/holds", headers=cust2,
                     json={"seat_ids": [target, all_ids[1]]})
    ok(r3.status_code == 409, "multi-seat hold is all-or-nothing (409)")
    free_check = client.get(f"/api/events/{event_id}/seatmap").json()
    status_of = {s["id"]: s["status"] for row in free_check["rows"] for s in row["seats"]}
    ok(status_of[all_ids[1]] == "available",
       "the free seat in the failed request stayed available (rollback)")

    print("\n[4] Hold TTL auto-release")
    time.sleep(1.2)          # TTL is 1 s in this test
    sweep()                  # sweeper flips the expired hold back
    status_of = {s["id"]: s["status"] for row in
                 client.get(f"/api/events/{event_id}/seatmap").json()["rows"]
                 for s in row["seats"]}
    ok(status_of[target] == "available", "expired hold auto-released to available")
    r = client.post(f"/api/events/{event_id}/holds", headers=cust2,
                    json={"seat_ids": [target]})
    ok(r.status_code == 201, "another customer can now hold that seat")
    client.delete(f"/api/events/{event_id}/holds", headers=cust2)  # tidy up

    print("\n[5] Booking + QR + verify")
    r = client.post(f"/api/events/{event_id}/holds", headers=cust1,
                    json={"seat_ids": all_ids})            # book ALL 4 seats
    ok(r.status_code == 201, "customer 1 holds all 4 seats")
    r = client.post("/api/bookings", headers=cust1,
                    json={"event_id": event_id, "seat_ids": all_ids})
    ok(r.status_code == 201, "booking confirmed from live holds")
    booking = r.json()
    ok(booking["total_amount"] == 2 * 500 + 2 * 200,
       "total priced per category (2×500 + 2×200)")
    r = client.get(f"/api/bookings/{booking['id']}/qr", headers=cust1)
    ok(r.status_code == 200 and r.headers["content-type"] == "image/png",
       "QR ticket PNG served")
    r = client.get(f"/api/verify/{booking['booking_ref']}", headers=organiser)
    ok(r.status_code == 200 and r.json()["valid"], "gate verify by booking_ref")
    r = client.post("/api/bookings", headers=cust2,
                    json={"event_id": event_id, "seat_ids": [target]})
    ok(r.status_code == 409, "booking without your own live hold is rejected")

    print("\n[6] Waitlist: join (sold out), cancel -> auto-offer")
    r = client.get(f"/api/events/{event_id}")
    ok(r.json()["sold_out"], "event is now sold out")
    r = client.post(f"/api/events/{event_id}/waitlist", headers=cust2,
                    json={"category": "Premium"})
    ok(r.status_code == 201 and r.json()["position"] == 1, "customer 2 joins waitlist (#1)")
    r = client.post(f"/api/events/{event_id}/waitlist", headers=cust3,
                    json={"category": "Premium"})
    ok(r.status_code == 201 and r.json()["position"] == 2, "customer 3 joins waitlist (#2)")
    r = client.post(f"/api/events/{event_id}/waitlist", headers=cust2,
                    json={"category": "Premium"})
    ok(r.status_code == 400, "duplicate waitlist join rejected")

    r = client.post(f"/api/bookings/{booking['id']}/cancel", headers=cust1)
    ok(r.status_code == 200 and r.json()["waitlist_offers_sent"] == 2,
       "cancellation freed 2 Premium seats -> 2 auto-offers (c2 & c3)")
    r = client.get("/api/me/waitlist", headers=cust2).json()
    ok(r[0]["status"] == "offered" and r[0].get("offer_token"),
       "customer 2 has a live time-limited offer")
    token_c2 = r[0]["offer_token"]

    print("\n[7] Offer expiry cascades to next in line")
    # c2 and c3 both hold offers for the 2 freed Premium seats. Let both lapse;
    # queue is empty behind them, so seats should return to open sale.
    time.sleep(1.2)
    sweep()
    r = client.get("/api/waitlist/offer/" + token_c2, headers=cust2)
    ok(r.status_code in (404, 410), "lapsed offer link is dead (404/410)")
    statuses = {e["status"] for e in client.get("/api/me/waitlist", headers=cust2).json()}
    ok("expired" in statuses, "customer 2's entry marked expired")
    premium = [c for c in client.get(f"/api/events/{event_id}").json()["categories"]
               if c["category"] == "Premium"][0]
    ok(premium["available"] == 2, "unclaimed seats returned to open sale")

    # Re-run the flow to prove the cascade ORDER. Book the 2 premium seats as
    # TWO separate bookings, queue c2 then c3, and cancel only ONE booking —
    # exactly one freed seat, two people in line.
    p1, p2 = all_ids[:2]                                   # the premium seats
    client.post(f"/api/events/{event_id}/holds", headers=cust1,
                json={"seat_ids": [p1]})
    bookingA = client.post("/api/bookings", headers=cust1,
                           json={"event_id": event_id, "seat_ids": [p1]}).json()
    client.post(f"/api/events/{event_id}/holds", headers=cust1,
                json={"seat_ids": [p2]})
    client.post("/api/bookings", headers=cust1,
                json={"event_id": event_id, "seat_ids": [p2]})
    client.post(f"/api/events/{event_id}/waitlist", headers=cust2,
                json={"category": "Premium"})
    client.post(f"/api/events/{event_id}/waitlist", headers=cust3,
                json={"category": "Premium"})
    r = client.post(f"/api/bookings/{bookingA['id']}/cancel", headers=cust1)
    ok(r.json()["waitlist_offers_sent"] == 1, "one freed seat -> exactly one offer")
    offers_c2 = [e for e in client.get("/api/me/waitlist", headers=cust2).json()
                 if e["status"] == "offered"]
    ok(len(offers_c2) == 1, "freed seat offered to #1 in line first (FIFO)")
    time.sleep(1.2)
    sweep()                                                # c2's offer lapses
    offers_c3 = [e for e in client.get("/api/me/waitlist", headers=cust3).json()
                 if e["status"] == "offered"]
    ok(len(offers_c3) == 1, "after expiry, the SAME seat cascades to #2 in line")

    print("\n[8] Claim: waitlisted customer completes the booking")
    token_c3 = offers_c3[0]["offer_token"]
    r = client.get("/api/waitlist/offer/" + token_c3, headers=cust2)
    ok(r.status_code == 403, "offer link is bound to the right customer (403 for others)")
    r = client.post(f"/api/waitlist/offer/{token_c3}/confirm", headers=cust3)
    ok(r.status_code == 201, "customer 3 books from the offer")
    entry = client.get("/api/me/waitlist", headers=cust3).json()[0]
    ok(entry["status"] == "converted", "waitlist entry marked converted")
    refs = [b["booking_ref"] for b in client.get("/api/me/bookings", headers=cust3).json()]
    ok(r.json()["booking_ref"] in refs, "booking appears in customer 3's history")

print(f"\nAll {PASSED} checks passed ✔")
TEST_DB.unlink(missing_ok=True)
