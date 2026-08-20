"""
App entrypoint.

- Creates tables on startup (SQLite-friendly; swap for Alembic in production).
- Starts the background sweeper that enforces hold TTLs and rotates
  expired waitlist offers.
- Serves the API under /api/* and the static frontend from /frontend
  (single service -> one URL to deploy on Render/Railway).
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import APP_NAME
from .database import Base, engine
from .routers import (admin_routes, auth_routes, booking_routes, event_routes,
                      hold_routes, waitlist_routes)
from .services.sweeper import sweeper_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)          # ensure schema exists
    task = asyncio.create_task(sweeper_loop())     # start TTL sweeper
    yield
    task.cancel()                                  # clean shutdown


app = FastAPI(title=APP_NAME, lifespan=lifespan)

app.add_middleware(   # permissive CORS: harmless same-origin, handy in dev
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_routes.router)
app.include_router(admin_routes.router)
app.include_router(event_routes.router)
app.include_router(hold_routes.router)
app.include_router(booking_routes.router)
app.include_router(waitlist_routes.router)

# Static frontend mounted last so /api/* keeps routing priority.
frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
