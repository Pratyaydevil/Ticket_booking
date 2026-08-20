"""
Database setup: engine, session factory, declarative base and the
FastAPI dependency that hands one session per request.
"""
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATABASE_URL

url = DATABASE_URL
# Render/Heroku style URLs use the old "postgres://" scheme; SQLAlchemy needs "postgresql://".
if url.startswith("postgres://"):
    url = url.replace("postgres://", "postgresql://", 1)

if url.startswith("sqlite"):
    # SQLite: allow use across FastAPI's threadpool workers.
    engine = create_engine(url, connect_args={"check_same_thread": False})
else:
    # Postgres etc.: pre_ping avoids stale connections on free-tier hosts.
    engine = create_engine(url, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def utcnow() -> datetime:
    """Naive UTC 'now' — all timestamps in the DB are naive UTC for portability."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_db():
    """FastAPI dependency: one DB session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
