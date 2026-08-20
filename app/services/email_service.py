"""
Email delivery with two modes:

- console (default): every email is printed to the server log AND written to
  ./outbox/ (body as .txt, QR as .png) so the whole flow is testable with
  zero credentials.
- smtp: real delivery through any free-tier SMTP relay (Brevo, Mailtrap,
  Gmail app password...) configured via env vars.

Failures are logged, never raised — email must not break a paid booking.
"""
import re
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path
from typing import List, Optional, Tuple

from .. import config

Attachment = Tuple[str, bytes, str, str]  # filename, data, maintype, subtype


def send_email(to: str, subject: str, body: str,
               attachments: Optional[List[Attachment]] = None) -> None:
    attachments = attachments or []
    try:
        if config.EMAIL_MODE == "smtp" and config.SMTP_HOST:
            _send_smtp(to, subject, body, attachments)
        else:
            _send_console(to, subject, body, attachments)
    except Exception as exc:  # noqa: BLE001 — email is best-effort by design
        print(f"[email] FAILED to={to} subject={subject!r}: {exc}")


def _send_smtp(to, subject, body, attachments):
    msg = EmailMessage()
    msg["From"] = config.EMAIL_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    for filename, data, maintype, subtype in attachments:
        msg.add_attachment(data, maintype=maintype, subtype=subtype,
                           filename=filename)
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
        smtp.send_message(msg)
    print(f"[email] sent via SMTP to={to} subject={subject!r}")


def _send_console(to, subject, body, attachments):
    outbox = Path(config.OUTBOX_DIR)
    outbox.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")[:40]
    base = outbox / f"{stamp}-{slug}"
    base.with_suffix(".txt").write_text(
        f"To: {to}\nSubject: {subject}\n\n{body}\n", encoding="utf-8")
    for filename, data, _, _ in attachments:
        (outbox / f"{stamp}-{filename}").write_bytes(data)
    print(f"[email] (console mode) to={to} subject={subject!r} "
          f"-> saved in {outbox}/")


# --------------------------- message templates --------------------------------

def send_booking_confirmation(booking, event, seat_labels, qr_png: bytes):
    body = (
        f"Hi {booking.customer.name},\n\n"
        f"Your booking is confirmed!\n\n"
        f"  Booking reference : {booking.booking_ref}\n"
        f"  Event             : {event.title} ({event.event_type})\n"
        f"  Venue             : {event.venue.name}, {event.venue.city}\n"
        f"  Starts            : {event.starts_at:%d %b %Y, %I:%M %p}\n"
        f"  Seats             : {', '.join(seat_labels)}\n"
        f"  Amount paid       : ₹{booking.total_amount}\n\n"
        f"Your QR code ticket is attached — show it at the gate.\n\n"
        f"— TicketBox"
    )
    send_email(booking.customer.email,
               f"Ticket confirmed — {booking.booking_ref}", body,
               [(f"ticket-{booking.booking_ref}.png", qr_png, "image", "png")])


def send_waitlist_offer(entry, event, seat_label: str, price: int):
    link = f"{config.BASE_URL}/claim.html?token={entry.offer_token}"
    minutes = config.WAITLIST_OFFER_TTL_SECONDS // 60
    body = (
        f"Hi {entry.customer.name},\n\n"
        f"Good news — a {entry.category} seat just opened up for:\n\n"
        f"  {event.title} at {event.venue.name}, {event.venue.city}\n"
        f"  Seat {seat_label} · ₹{price}\n\n"
        f"It is reserved for you for the next {minutes} minutes. "
        f"Complete your booking here:\n\n  {link}\n\n"
        f"If you don't book by {entry.offer_expires_at:%I:%M %p} (UTC), the "
        f"seat is offered to the next person in line.\n\n— TicketBox"
    )
    send_email(entry.customer.email,
               f"Seat available — {event.title} (act fast!)", body)


def send_cancellation_note(booking, event):
    body = (
        f"Hi {booking.customer.name},\n\n"
        f"Your booking {booking.booking_ref} for {event.title} has been "
        f"cancelled. The seats have been released.\n\n— TicketBox"
    )
    send_email(booking.customer.email,
               f"Booking cancelled — {booking.booking_ref}", body)
