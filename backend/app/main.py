import logging
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import jwt
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from . import logic
from .config import get_secret_key, get_settings
from .db import Base, SessionLocal, engine, utcnow
from .models import User
from .routes_admin import admin, auth_router, record_audit
from .routes_device import router as device_router
from .security import hash_password

log = logging.getLogger("noparking")


def _add_missing_columns():
    """Tiny migration for existing SQLite files: create_all() never adds columns to existing tables."""
    from sqlalchemy import inspect, text

    cols = {c["name"] for c in inspect(engine).get_columns("devices")}
    if "last_ip" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE devices ADD COLUMN last_ip VARCHAR(45)"))


def init_db():
    Base.metadata.create_all(engine)
    _add_missing_columns()
    s = get_settings()
    with SessionLocal() as db:
        email = s.admin_email.strip().lower()
        if not db.scalar(select(User).where(User.email == email)):
            pw = s.admin_password or secrets.token_urlsafe(12)
            db.add(User(email=email, password_hash=hash_password(pw), role="ADMIN"))
            db.commit()
            if not s.admin_password:
                print(f"\n*** First start: admin login = {email} / {pw}  (shown once; set ADMIN_PASSWORD to choose your own) ***\n")


def _tick():
    try:
        with SessionLocal() as db:
            logic.evaluate_sessions(db, utcnow())
            logic.expire_leases(db, utcnow())
    except Exception:  # keep the scheduler alive
        log.exception("scheduler tick failed")


def _anpr_tick():
    try:
        from .anpr_worker import process_pending

        with SessionLocal() as db:
            process_pending(db)
    except Exception:
        log.exception("ANPR tick failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    sched = None
    s = get_settings()
    if s.scheduler_enabled:
        # State lives in the database, so a restart never resets a timer.
        sched = BackgroundScheduler(timezone="UTC")
        sched.add_job(_tick, "interval", seconds=s.scheduler_interval_seconds, max_instances=1, coalesce=True)
        # Separate job so slow OCR can never delay the violation timer above.
        sched.add_job(_anpr_tick, "interval", seconds=s.scheduler_interval_seconds, max_instances=1, coalesce=True)
        sched.start()
    yield
    if sched:
        sched.shutdown(wait=False)


app = FastAPI(title="Smart No-Parking Alert System", version="2.0.0", lifespan=lifespan)
_AUDIT_LABELS = [
    ("POST", r"^/api/v1/vehicles$", "Added vehicle"),
    ("PUT", r"^/api/v1/vehicles/[^/]+$", "Edited vehicle"),
    ("DELETE", r"^/api/v1/vehicles/[^/]+$", "Deactivated vehicle"),
    ("POST", r"^/api/v1/zones$", "Created zone"),
    ("POST", r"^/api/v1/devices$", "Registered camera unit"),
    ("PUT", r"^/api/v1/settings$", "Changed settings"),
    ("POST", r"^/api/v1/detections/[^/]+/review$", "Confirmed a plate by hand"),
    ("POST", r"^/api/v1/detections/[^/]+/reject$", "Marked a photo as not a plate"),
    ("POST", r"^/api/v1/notifications/[^/]+/retry$", "Retried an SMS"),
    ("POST", r"^/api/v1/account/password$", "Changed password"),
]


@app.middleware("http")
async def audit_changes(request, call_next):
    response = await call_next(request)
    try:
        if request.method in ("POST", "PUT", "DELETE") and response.status_code < 400:
            for method, pattern, label in _AUDIT_LABELS:
                if request.method == method and re.match(pattern, request.url.path):
                    actor = "admin"
                    auth = request.headers.get("authorization", "")
                    if auth.lower().startswith("bearer "):
                        try:
                            actor = jwt.decode(auth[7:], get_secret_key(get_settings()), algorithms=["HS256"]).get("email", "admin")
                        except jwt.PyJWTError:
                            pass
                    record_audit(actor, label, request.url.path.removeprefix("/api/v1")[:120])
                    break
    except Exception:  # auditing must never break a request
        log.exception("audit failed")
    return response


app.add_middleware(CORSMiddleware, allow_origins=get_settings().cors_origins, allow_methods=["*"], allow_headers=["*"])
app.include_router(auth_router)
app.include_router(admin)
app.include_router(device_router)


@app.get("/api/v1/health", tags=["health"])
def health():
    return {"status": "ok", "time": utcnow().isoformat()}


# Dashboard: a single static page served by the backend (same origin, no build step). Mounted last
# so every /api route above wins.
_STATIC = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=_STATIC, html=True), name="dashboard")
