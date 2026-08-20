# System Design Write-up — TicketBox

## Seat hold and TTL mechanism

The seat map's source of truth is one table, `event_seats`: when an organiser
creates an event, one row is materialised per physical seat of the venue, and
that row carries the per-show state — `status` (available / held / booked),
`held_by_id`, `hold_kind` (cart or waitlist_offer) and `hold_expires_at`.
Holding a seat means flipping its row to `held` with
`hold_expires_at = now + SEAT_HOLD_TTL_SECONDS` (default 600, configurable via
env). The hold is what the customer "owns" during checkout; booking converts
it, abandoning it lets it lapse.

TTL expiry is enforced in two complementary layers, so correctness never
depends on a timer actually firing:

1. **Lazy checks** — every read and every state-changing query treats a hold
   with `hold_expires_at <= now` as available. The seat-map endpoint reports
   such seats as available, and the hold query is allowed to steal them.
2. **Active sweeper** — an asyncio task inside the FastAPI process wakes every
   `SWEEP_INTERVAL_SECONDS` (10 s) and flips lapsed cart holds back to
   `available`. This is what makes abandonment visible in near real time: the
   frontend polls the seat map every 5 s, so within seconds of a hold
   expiring, other customers see the seat free again.

The sweeper is an in-process scheduler rather than a database-level job
(pg_cron, Redis key expiry) to keep the system portable across SQLite and
free-tier Postgres; the lazy layer means a crashed sweeper degrades liveness,
never correctness.

## Concurrency prevention

Every transition on a seat is an **atomic conditional UPDATE** — a
compare-and-set where the precondition lives in the WHERE clause:

```sql
UPDATE event_seats
   SET status='held', held_by_id=:me, hold_expires_at=:exp
 WHERE id=:seat
   AND (status='available'
        OR (status='held' AND hold_expires_at <= :now));
```

If two customers race for the same seat, the database serialises the writes:
in PostgreSQL the second UPDATE blocks on the row lock, then re-evaluates the
WHERE clause against the committed row and matches zero rows; in SQLite,
writers are fully serialised. The application simply checks `rowcount` — 1
means won, 0 means lost — so simultaneous attempts can never both succeed,
without any explicit `SELECT ... FOR UPDATE` or application-level locks.

Multi-seat requests are all-or-nothing: seats are claimed one conditional
UPDATE at a time inside a single transaction, and any rowcount-0 rolls the
whole transaction back, returning HTTP 409 with the contested seat ids.
Booking uses the same pattern with a stricter precondition —
`status='held' AND held_by_id=:me AND hold_expires_at > :now` — so an expired
or foreign hold can never be converted into a booking, even if the request
arrives a millisecond after expiry. `UNIQUE(event_id, seat_id)` guarantees a
single authoritative row per seat per show.

## Waitlist auto-assignment flow

The waitlist is a strict FIFO queue per (event, seat category), stored as
`waitlist_entries` ordered by `created_at`. Customers may join only when that
category is genuinely sold out (checked with the same lazy-TTL view), and
duplicate active entries are rejected.

When a booking is cancelled, each freed seat is routed through
`offer_seat_to_next()` inside the cancellation transaction: the head WAITING
entry is found, the seat is immediately re-held for that customer
(`hold_kind='waitlist_offer'`, expiry = `WAITLIST_OFFER_TTL_SECONDS`, default
15 min), and the entry becomes OFFERED with a fresh unguessable
`offer_token`. After commit, the customer is emailed a claim link containing
the token. Re-holding the seat is the key design point: the offer is not a
notification to race for the seat, it is exclusive dibs — nobody else can take
it while the offer is live. If the queue is empty, the seat simply returns to
open sale. When a multi-seat cancellation frees several seats, each is
assigned to a successive queue member (the assignment flushes per seat so the
head query always sees the previous offer).

## Time-limited offer handling

The claim link opens a countdown page (server-time-synced) showing the
reserved seat and price; confirming runs the standard hold→booking
compare-and-set bound to the offer's customer, marking the entry CONVERTED.
Expiry is again double-enforced: the sweeper marks lapsed offers EXPIRED and
calls `offer_seat_to_next()` on the same seat, cascading it down the queue —
each successor gets a fresh full TTL and email — until someone books or the
queue empties; and any attempt to view or confirm a lapsed token triggers the
same rotation lazily and returns 410. Tokens are single-purpose, invalidated
on expiry/conversion, and authorised against the logged-in account, so a
forwarded email cannot leak the seat.

*(~740 words)*
