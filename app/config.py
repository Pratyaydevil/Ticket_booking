"""
Central configuration.

Every tunable lives here and is read from environment variables
(with sensible defaults for local development), so the same code
runs locally and on Render/Railway without edits.
"""
import os

from dotenv import load_dotenv

load_dotenv()  # reads a local .env file if present


def _int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))


APP_NAME = os.getenv("APP_NAME", "TicketBox — Ticket Booking System")

# --- Security ---------------------------------------------------------------
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me-this-is-not-for-production-use")   # JWT signing key
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = _int("JWT_EXPIRE_MINUTES", 24 * 60)       # token lifetime

# --- Database ---------------------------------------------------------------
# SQLite by default; set DATABASE_URL=postgresql://... in production.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./tbs.db")

# --- Seat hold / waitlist timing (the heart of the assignment) --------------
SEAT_HOLD_TTL_SECONDS = _int("SEAT_HOLD_TTL_SECONDS", 600)         # 10 min hold
WAITLIST_OFFER_TTL_SECONDS = _int("WAITLIST_OFFER_TTL_SECONDS", 900)  # 15 min offer
SWEEP_INTERVAL_SECONDS = _int("SWEEP_INTERVAL_SECONDS", 10)        # sweeper cadence

# --- Email ------------------------------------------------------------------
# EMAIL_MODE=console  -> emails are printed + written to ./outbox (no creds needed)
# EMAIL_MODE=smtp     -> real emails via any free-tier SMTP (Brevo, Mailtrap, Gmail app password)
EMAIL_MODE = os.getenv("EMAIL_MODE", "console")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = _int("SMTP_PORT", 587)
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "tickets@ticketbox.local")
OUTBOX_DIR = os.getenv("OUTBOX_DIR", "./outbox")

# --- Misc -------------------------------------------------------------------
# Public base URL of the deployed app; used to build waitlist-offer links in emails.
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
