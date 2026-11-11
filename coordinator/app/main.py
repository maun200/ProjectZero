"""ProjectZero coordinator (formerly Community AI Grid): deliberately limited, experimental."""
import asyncio
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import community
from .database import Database, digest
from .ratelimit import RateLimiter

COST = 5  # demo credits per completed job; *not* a market price or verified computation
MAX_ATTEMPTS = 3
MAX_BODY_BYTES = 1024 * 1024  # largest legitimate payload (100k-char completion) stays well below
MODEL_NAME = re.compile(r"^[A-Za-z0-9_.:-]+$")


def now() -> datetime:
    return datetime.now(timezone.utc)


def ts(value: datetime | None = None) -> str:
    return (value or now()).isoformat(timespec="microseconds")


def make_enroll_code() -> str:
    """Opaque single-use self-enrollment code; the coordinator tracks redemption in `enroll_codes`.

    Superseded the earlier stateless HMAC-over-expiry code (still reachable in git history) once
    a device-facing threat model requires real single-use tracking, not just a time limit — see
    docs/OPEN_ENROLLMENT_SPIKE.md "What this is not" #2.
    """
    return secrets.token_urlsafe(24)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def requeue_or_fail(db: sqlite3.Connection, job: sqlite3.Row, error: str, timestamp: str,
                    refund_note: str) -> Literal["queued", "failed"]:
    """Give a job back to the queue, or fail and refund it once attempts are exhausted.

    Must run inside an open write transaction.
    """
    if job["attempts"] < MAX_ATTEMPTS:
        db.execute("""UPDATE jobs SET status='queued',error=?,assigned_worker=NULL,lease_hash=NULL,
            lease_until=NULL,updated_at=? WHERE id=?""", (error[:500], timestamp, job["id"]))
        return "queued"
    db.execute("""UPDATE jobs SET status='failed',error=?,lease_hash=NULL,
        lease_until=NULL,updated_at=? WHERE id=?""", (error[:500], timestamp, job["id"]))
    db.execute("UPDATE users SET credits=credits+? WHERE id=?", (job["reservation"], job["user_id"]))
    db.execute("INSERT INTO ledger(actor_type,actor_id,delta,job_id,note,created_at) VALUES ('user',?,?,?,?,?)",
               (job["user_id"], job["reservation"], job["id"], refund_note, timestamp))
    return "failed"


def release_expired(db: sqlite3.Connection, timestamp: str):
    """Requeue jobs whose worker lease ran out; fail and refund those out of attempts.

    Must run inside an open write transaction.
    """
    expired = db.execute("SELECT id,user_id,reservation,attempts FROM jobs WHERE status='running' AND lease_until<=?",
                         (timestamp,)).fetchall()
    for old in expired:
        requeue_or_fail(db, old, "Worker lease expired", timestamp, "refund_expired_job")


def expire_stale_queued(db: sqlite3.Connection, timestamp: str, cutoff: str) -> int:
    """Fail and refund jobs waiting in the queue since `cutoff` or earlier; no matching worker took them.

    For a queued job `updated_at` is when it (re-)entered the queue, so time spent running on a
    worker that later failed does not count against the timeout.

    Must run inside an open write transaction.
    """
    stale = db.execute("SELECT id,user_id,reservation FROM jobs WHERE status='queued' AND updated_at<=?",
                       (cutoff,)).fetchall()
    for job in stale:
        db.execute("""UPDATE jobs SET status='failed',error='Queue timeout: no matching worker',
            updated_at=? WHERE id=?""", (timestamp, job["id"]))
        db.execute("UPDATE users SET credits=credits+? WHERE id=?", (job["reservation"], job["user_id"]))
        db.execute("INSERT INTO ledger(actor_type,actor_id,delta,job_id,note,created_at) VALUES ('user',?,?,?,'refund_queue_timeout',?)",
                   (job["user_id"], job["reservation"], job["id"], timestamp))
    return len(stale)


def sweep(db: sqlite3.Connection, current: datetime, queue_timeout_seconds: int) -> None:
    """Apply lease expiry and queue timeout. Must run inside an open write transaction."""
    timestamp = ts(current)
    release_expired(db, timestamp)
    expire_stale_queued(db, timestamp, ts(current - timedelta(seconds=queue_timeout_seconds)))


def sweep_once(database: Database, queue_timeout_seconds: int) -> None:
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        # Sample time after acquiring the writer lock, as in the request paths.
        sweep(db, now(), queue_timeout_seconds)
        db.commit()


@dataclass(frozen=True)
class Settings:
    admin_key: str
    user_key: str
    db_path: str
    initial_credits: int = 100
    lease_seconds: int = 180
    queue_timeout_seconds: int = 3600
    sweep_seconds: float = 0.0  # 0 disables the background sweeper (test default)
    allowed_models: tuple[str, ...] = ("demo-model",)
    rate_limit_per_minute: int = 0  # 0 disables the limiter (test default)
    registration: str = "off"  # off | invite | open
    signup_credits: int = 20
    auto_settle: bool = False  # credit completed jobs directly (unverified!) instead of provisionally
    register_per_hour: int = 10  # per source address
    max_machines_per_member: int = 5
    enroll_ip_per_minute: int = 0  # 0 disables the per-source /api/enroll limiter (test default)

    @classmethod
    def from_env(cls):
        return cls(
            admin_key=os.getenv("CAG_ADMIN_KEY", ""),
            user_key=os.getenv("CAG_USER_KEY", ""),
            db_path=os.getenv("CAG_DB_PATH", "/data/grid.sqlite3"),
            initial_credits=int(os.getenv("CAG_INITIAL_DEMO_CREDITS", "100")),
            lease_seconds=int(os.getenv("CAG_LEASE_SECONDS", "180")),
            queue_timeout_seconds=int(os.getenv("CAG_QUEUE_TIMEOUT_SECONDS", "3600")),
            sweep_seconds=float(os.getenv("CAG_SWEEP_SECONDS", "30")),
            allowed_models=tuple(m.strip() for m in os.getenv("CAG_ALLOWED_MODELS", "demo-model").split(",")
                                 if m.strip()),
            rate_limit_per_minute=int(os.getenv("CAG_RATE_LIMIT_PER_MINUTE", "120")),
            registration=os.getenv("CAG_REGISTRATION", "off").strip().lower(),
            signup_credits=int(os.getenv("CAG_SIGNUP_CREDITS", "20")),
            auto_settle=os.getenv("CAG_AUTO_SETTLE", "false").strip().lower() in {"1", "true", "yes", "on"},
            register_per_hour=int(os.getenv("CAG_REGISTER_PER_HOUR", "10")),
            max_machines_per_member=int(os.getenv("CAG_MAX_MACHINES_PER_MEMBER", "5")),
            enroll_ip_per_minute=int(os.getenv("CAG_ENROLL_IP_PER_MINUTE", "5")),
        )

    def validate(self):
        if (len(self.admin_key) < 24 or len(self.user_key) < 24 or
            any("REPLACE_" in x or "CHANGEME" in x.upper() for x in (self.admin_key, self.user_key))):
            raise RuntimeError("Both CAG_ADMIN_KEY and CAG_USER_KEY must be independently generated 24+ character secrets (no placeholders)")
        if hmac.compare_digest(self.admin_key, self.user_key):
            raise RuntimeError("Admin and user keys must be different")
        if self.initial_credits < 0:
            raise RuntimeError("Initial demo balance must be nonnegative")
        if not 5 <= self.lease_seconds <= 3600:
            raise RuntimeError("CAG_LEASE_SECONDS must be between 5 and 3600")
        if not 60 <= self.queue_timeout_seconds <= 604800:
            raise RuntimeError("CAG_QUEUE_TIMEOUT_SECONDS must be between 60 and 604800")
        if not (self.sweep_seconds == 0 or 0.2 <= self.sweep_seconds <= 3600):
            raise RuntimeError("CAG_SWEEP_SECONDS must be 0 or between 0.2 and 3600")
        if not self.allowed_models or not all(MODEL_NAME.match(m) for m in self.allowed_models):
            raise RuntimeError("CAG_ALLOWED_MODELS must list at least one valid model name")
        if not 0 <= self.rate_limit_per_minute <= 10000:
            raise RuntimeError("CAG_RATE_LIMIT_PER_MINUTE must be between 0 and 10000")
        if self.registration not in {"off", "invite", "open"}:
            raise RuntimeError("CAG_REGISTRATION must be off, invite or open")
        if not 0 <= self.signup_credits <= 10000:
            raise RuntimeError("CAG_SIGNUP_CREDITS must be between 0 and 10000")
        if not 1 <= self.register_per_hour <= 10000:
            raise RuntimeError("CAG_REGISTER_PER_HOUR must be between 1 and 10000")
        if not 1 <= self.max_machines_per_member <= 100:
            raise RuntimeError("CAG_MAX_MACHINES_PER_MEMBER must be between 1 and 100")
        if not 0 <= self.enroll_ip_per_minute <= 10000:
            raise RuntimeError("CAG_ENROLL_IP_PER_MINUTE must be between 0 and 10000")


class BodySizeLimit:
    """Reject oversized bodies; FastAPI parses the body *before* auth dependencies run."""

    def __init__(self, app, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope["headers"]).get(b"content-length", b"0")
        if declared.isdigit() and int(declared) > self.limit:
            return await JSONResponse({"detail": "Request body too large"}, status_code=413)(scope, receive, send)
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > self.limit:  # chunked uploads without Content-Length
                raise HTTPException(413, "Request body too large")
            return message

        await self.app(scope, limited_receive, send)


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=16000)


class JobIn(BaseModel):
    model: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")
    messages: list[Message] = Field(min_length=1, max_length=16)
    max_output_tokens: int = Field(default=128, ge=1, le=512)
    privacy: Literal["trusted", "community"] = "trusted"
    allow_untrusted: bool = False

    @model_validator(mode="after")
    def consent(self):
        if self.privacy == "community" and not self.allow_untrusted:
            raise ValueError("Community mode requires explicit allow_untrusted=true; do not send sensitive prompts")
        if sum(len(m.content) for m in self.messages) > 24000:
            raise ValueError("Combined prompt too large")
        return self


class WorkerIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")
    trust_level: Literal["trusted", "community"] = "trusted"


class WorkerModelIn(BaseModel):
    model: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")


class EnrollCodeIn(BaseModel):
    ttl_seconds: int = Field(default=3600, ge=60, le=86400)


class SelfEnrollIn(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")
    model: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")
    code: str = Field(min_length=1, max_length=120)


class Settlement(BaseModel):
    credits: int = Field(ge=1, le=10000)
    audit_note: str = Field(min_length=8, max_length=500)


class Capabilities(BaseModel):
    """Known worker metadata only; unknown keys such as hostnames are dropped."""
    model_config = ConfigDict(extra="ignore")
    platform: str | None = Field(None, max_length=40)
    architecture: str | None = Field(None, max_length=40)
    cpu_logical: int | None = Field(None, ge=1, le=4096)
    ram_bytes: int | None = Field(None, ge=0)
    cpu_percent: float | None = Field(None, ge=0, le=100)
    nvidia_smi_available: bool | None = None
    local_implementation: Literal["openai", "ollama", "openai-compatible", "mock"] | None = None


class Heartbeat(BaseModel):
    capabilities: Capabilities = Field(default_factory=Capabilities)
    job_id: str | None = None
    lease_token: str | None = None


class Completion(BaseModel):
    job_id: str
    lease_token: str
    ok: bool
    content: str = Field(default="", max_length=100000)
    error: str = Field(default="", max_length=1000)
    usage: dict = Field(default_factory=dict)


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings.from_env()
    database = Database(cfg.db_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        cfg.validate()
        database.initialize(cfg.user_key, cfg.initial_credits)

        async def sweeper():
            while True:
                await asyncio.sleep(cfg.sweep_seconds)
                try:
                    await asyncio.to_thread(sweep_once, database, cfg.queue_timeout_seconds)
                except sqlite3.OperationalError as exc:  # e.g. "database is locked": retry next round
                    print(f"Sweep skipped: {exc}", file=sys.stderr)

        task = asyncio.create_task(sweeper()) if cfg.sweep_seconds else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="ProjectZero coordinator (experimental)", version="0.3.0", lifespan=lifespan)
    app.add_middleware(BodySizeLimit, limit=MAX_BODY_BYTES)
    app.state.database = database

    def bearer(authorization: str | None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Bearer token required")
        return authorization[7:]

    limiter = RateLimiter(cfg.rate_limit_per_minute)
    enroll_ip_limiter = RateLimiter(cfg.enroll_ip_per_minute)

    def throttle(key: str):
        # Only authenticated identities are counted; invalid tokens never use up a real budget.
        wait = limiter.check(key)
        if wait > 0:
            raise HTTPException(429, "Rate limit exceeded", headers={"Retry-After": str(math.ceil(wait))})

    def admin(authorization: str | None = Header(default=None)):
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input.
        if not hmac.compare_digest(bearer(authorization).encode("utf-8"), cfg.admin_key.encode("utf-8")):
            raise HTTPException(status_code=403, detail="Invalid administrator token")
        throttle("admin")

    def user(authorization: str | None = Header(default=None)) -> str:
        token_hash = digest(bearer(authorization))
        with database.connect() as db:
            row = db.execute("SELECT id FROM users WHERE api_key_hash=?", (token_hash,)).fetchone()
        if row is None:
            raise HTTPException(status_code=403, detail="Invalid user token")
        throttle("user:" + row["id"])
        return row["id"]

    def worker(authorization: str | None = Header(default=None)) -> sqlite3.Row:
        token_hash = digest(bearer(authorization))
        with database.connect() as db:
            row = db.execute("SELECT * FROM workers WHERE token_hash=? AND revoked_at IS NULL",
                             (token_hash,)).fetchone()
        if row is None:
            raise HTTPException(status_code=403, detail="Invalid worker token")
        throttle("worker:" + row["id"])
        return row

    def reauthorize(db: sqlite3.Connection, w: sqlite3.Row):
        """Recheck the worker token inside the write transaction.

        Authentication ran before the writer lock; a revocation or rotation may have committed
        while this request waited for it.
        """
        if db.execute("SELECT 1 FROM workers WHERE id=? AND token_hash=? AND revoked_at IS NULL",
                      (w["id"], w["token_hash"])).fetchone() is None:
            db.rollback()
            raise HTTPException(status_code=403, detail="Invalid worker token")

    def approved(model: str):
        if model not in cfg.allowed_models:
            raise HTTPException(422, "Model not approved by operator")

    def revoke_worker(db: sqlite3.Connection, worker_id: str, timestamp: str) -> dict:
        """Revoke inside an open write transaction; running jobs are requeued or failed with refund."""
        db.execute("UPDATE workers SET revoked_at=? WHERE id=?", (timestamp, worker_id))
        running = db.execute("SELECT * FROM jobs WHERE status='running' AND assigned_worker=?",
                             (worker_id,)).fetchall()
        outcomes = [requeue_or_fail(db, job, "Worker revoked", timestamp, "refund_revoked_worker_job")
                    for job in running]
        return {"worker_id": worker_id, "revoked": True, "requeued_jobs": outcomes.count("queued"),
                "failed_jobs": outcomes.count("failed")}

    community.install(app, community.Context(
        database=database, throttle=throttle, approved=approved, admin=admin, now=lambda: now(), ts=ts,
        revoke_worker=revoke_worker, registration=cfg.registration, signup_credits=cfg.signup_credits,
        auto_settle=cfg.auto_settle, register_per_hour=cfg.register_per_hour,
        max_machines=cfg.max_machines_per_member, cost=COST))

    @app.get("/")
    def frontend():
        return FileResponse(Path(__file__).with_name("index.html"))

    @app.get("/health")
    def health():
        return {"status": "ok", "network": "private-poc", "verified_compute": False, "version": "0.3.0"}

    @app.get("/api/balance")
    def balance(identity: str = Depends(user)):
        with database.connect() as db:
            row = db.execute("SELECT credits FROM users WHERE id=?", (identity,)).fetchone()
        return {"user": identity, "demo_credits": row["credits"], "note": "Non-transferable experimental accounting"}

    @app.post("/api/jobs", status_code=201)
    def submit(payload: JobIn, identity: str = Depends(user)):
        approved(payload.model)
        ident, timestamp = str(uuid.uuid4()), ts()
        with database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            change = db.execute("UPDATE users SET credits=credits-? WHERE id=? AND credits>=?", (COST, identity, COST))
            if change.rowcount != 1:
                db.rollback()
                raise HTTPException(status_code=402, detail="Insufficient demo credits")
            db.execute("""INSERT INTO jobs(id,user_id,model,messages_json,privacy,max_output_tokens,status,created_at,updated_at,reservation)
                VALUES (?,?,?,?,?,?,'queued',?,?,?)""",
                (ident, identity, payload.model, json.dumps([m.model_dump() for m in payload.messages]),
                 payload.privacy, payload.max_output_tokens, timestamp, timestamp, COST))
            db.execute("INSERT INTO ledger(actor_type,actor_id,delta,job_id,note,created_at) VALUES ('user',?,?,?,'reserved_demo_job',?)",
                       (identity, -COST, ident, timestamp))
            db.commit()
        return {"id": ident, "status": "queued", "demo_cost": COST}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str, identity: str = Depends(user)):
        with database.connect() as db:
            # Expiry must not depend on some worker polling /claim, or reservations stay locked.
            db.execute("BEGIN IMMEDIATE")
            sweep(db, now(), cfg.queue_timeout_seconds)
            db.commit()
            row = db.execute("SELECT * FROM jobs WHERE id=? AND user_id=?", (job_id, identity)).fetchone()
        if row is None:
            raise HTTPException(404, "Job not found")
        return {"id": row["id"], "status": row["status"], "model": row["model"],
                "privacy": row["privacy"], "attempts": row["attempts"],
                "output": json.loads(row["result_json"]) if row["result_json"] else None,
                "error": row["error"] if row["status"] in ("failed", "cancelled") else None}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str, identity: str = Depends(user)):
        with database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            timestamp = ts()
            row = db.execute("SELECT status,reservation FROM jobs WHERE id=? AND user_id=?",
                             (job_id, identity)).fetchone()
            if row is None:
                db.rollback()
                raise HTTPException(404, "Job not found")
            if row["status"] not in ("queued", "running"):
                db.rollback()
                raise HTTPException(409, "Job already finished")
            # Clearing the lease makes a late heartbeat or completion from the worker fail with 409.
            db.execute("""UPDATE jobs SET status='cancelled',error='Cancelled by user',lease_hash=NULL,
                lease_until=NULL,updated_at=? WHERE id=?""", (timestamp, job_id))
            db.execute("UPDATE users SET credits=credits+? WHERE id=?", (row["reservation"], identity))
            db.execute("INSERT INTO ledger(actor_type,actor_id,delta,job_id,note,created_at) VALUES ('user',?,?,?,'refund_cancelled_job',?)",
                       (identity, row["reservation"], job_id, timestamp))
            db.commit()
        return {"id": job_id, "status": "cancelled", "refunded": row["reservation"]}

    @app.get("/api/public/config")
    def public_config():
        return {"allowed_models": list(cfg.allowed_models), "registration": cfg.registration,
                "signup_credits": cfg.signup_credits, "job_cost": COST, "auto_settle": cfg.auto_settle}

    @app.get("/api/stats")
    def stats():
        with database.connect() as db:
            workers = db.execute(
                "SELECT trust_level, COUNT(*) c FROM workers WHERE revoked_at IS NULL GROUP BY trust_level"
            ).fetchall()
            jobs = db.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status").fetchall()
            members = db.execute("SELECT COUNT(*) c FROM users WHERE name IS NOT NULL").fetchone()["c"]
            online = db.execute("SELECT COUNT(*) c FROM workers WHERE revoked_at IS NULL AND last_seen>?",
                                (ts(now() - timedelta(seconds=community.ONLINE_SECONDS)),)).fetchone()["c"]
        return {"active_workers": {r["trust_level"]: r["c"] for r in workers},
                "jobs": {r["status"]: r["c"] for r in jobs}, "members": members, "online_machines": online}

    @app.post("/api/admin/enroll-codes", dependencies=[Depends(admin)])
    def issue_enroll_code(payload: EnrollCodeIn):
        code = make_enroll_code()
        timestamp = ts()
        expires_at = ts(now() + timedelta(seconds=payload.ttl_seconds))
        with database.connect() as db:
            db.execute("INSERT INTO enroll_codes(code_hash,expires_at,used_at,created_at) VALUES (?,?,NULL,?)",
                       (digest(code), expires_at, timestamp))
        return {"code": code, "expires_at": expires_at,
                "note": "Single-use: the first device to redeem it consumes it; share with exactly one device."}

    @app.post("/api/enroll", status_code=201)
    def self_enroll(payload: SelfEnrollIn, request: Request):
        """Public self-enrollment for untrusted `community` workers using an operator-issued code.

        Structural spike for open/public participation: no SSH key, no coordinator-host step.
        The device gets worker_token + user_token directly and talks to this coordinator's own
        reachable address (LAN/Tailscale) from here on. Always enrolls as `community`, never
        `trusted` — that distinction stays admin-only via /api/admin/workers.

        Hardened 2026-11-05 per docs/OPEN_ENROLLMENT_SPIKE.md's own "next steps": codes are now
        single-use (tracked in `enroll_codes`, consumed atomically with worker creation so a race
        between two redeemers cannot both succeed) and throttled per source IP in addition to the
        pre-existing shared bucket. Still not a full abuse-control story: IPs behind the same NAT
        or proxy share one bucket, and there is still no CAPTCHA or proof of real compute — see
        docs/SECURITY.md "Known unfinished items required before public launch".
        """
        throttle("enroll")  # shared backstop bucket, kept alongside the new per-IP one below
        ip_wait = enroll_ip_limiter.check("ip:" + client_ip(request))
        if ip_wait > 0:
            raise HTTPException(429, "Too many enrollment attempts from this source",
                                headers={"Retry-After": str(math.ceil(ip_wait))})
        approved(payload.model)
        code_hash = digest(payload.code)
        timestamp = ts()
        ident, token = str(uuid.uuid4()), "cag_" + secrets.token_urlsafe(32)
        user_token = "cag_usr_" + secrets.token_urlsafe(32)
        with database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT expires_at, used_at FROM enroll_codes WHERE code_hash=?",
                             (code_hash,)).fetchone()
            if row is None or row["used_at"] is not None or row["expires_at"] <= timestamp:
                db.rollback()
                raise HTTPException(403, "Invalid, already-used, or expired enrollment code")
            db.execute("UPDATE enroll_codes SET used_at=? WHERE code_hash=?", (timestamp, code_hash))
            db.execute("INSERT INTO workers(id,name,token_hash,model,trust_level) VALUES (?,?,?,?,?)",
                       (ident, payload.name, digest(token), payload.model, "community"))
            db.execute("INSERT INTO users(id,api_key_hash,credits) VALUES (?,?,0)", (ident, digest(user_token)))
            db.commit()
        return {"id": ident, "name": payload.name, "model": payload.model, "trust_level": "community",
                "worker_token": token, "user_token": user_token,
                "warning": "Worker and user tokens shown once. Store both now."}

    @app.post("/api/admin/workers", status_code=201, dependencies=[Depends(admin)])
    def enroll(payload: WorkerIn):
        approved(payload.model)
        ident, token = str(uuid.uuid4()), "cag_" + secrets.token_urlsafe(32)
        user_token = "cag_usr_" + secrets.token_urlsafe(32)
        with database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO workers(id,name,token_hash,model,trust_level) VALUES (?,?,?,?,?)",
                       (ident, payload.name, digest(token), payload.model, payload.trust_level))
            db.execute("INSERT INTO users(id,api_key_hash,credits) VALUES (?,?,0)", (ident, digest(user_token)))
            db.commit()
        return {"id": ident, "name": payload.name, "model": payload.model, "trust_level": payload.trust_level,
                "worker_token": token, "user_token": user_token,
                "warning": "Worker and user tokens shown once. Store separately and securely. Never commit to Git."}

    @app.get("/api/admin/workers", dependencies=[Depends(admin)])
    def list_workers():
        with database.connect() as db:
            rows = db.execute("SELECT id,name,model,trust_level,provisional_credits,last_seen,revoked_at,capabilities_json FROM workers ORDER BY name").fetchall()
        return [{**dict(r), "capabilities": json.loads(r["capabilities_json"])} for r in rows]

    @app.post("/api/admin/workers/{worker_id}/revoke", dependencies=[Depends(admin)])
    def revoke(worker_id: str):
        """Disable a worker token at once; its running jobs go back to the queue or fail with refund."""
        with database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            timestamp = ts()
            row = db.execute("SELECT revoked_at FROM workers WHERE id=?", (worker_id,)).fetchone()
            if row is None:
                db.rollback()
                raise HTTPException(404, "Unknown worker")
            if row["revoked_at"]:
                db.rollback()
                raise HTTPException(409, "Worker already revoked")
            result = revoke_worker(db, worker_id, timestamp)
            db.commit()
        # The worker's separate user token stays valid so already settled credits remain visible.
        return result

    @app.post("/api/admin/workers/{worker_id}/rotate", dependencies=[Depends(admin)])
    def rotate(worker_id: str):
        token = "cag_" + secrets.token_urlsafe(32)
        with database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revoked_at FROM workers WHERE id=?", (worker_id,)).fetchone()
            if row is None:
                db.rollback()
                raise HTTPException(404, "Unknown worker")
            if row["revoked_at"]:
                db.rollback()
                raise HTTPException(409, "Worker revoked")
            # Leases belong to the worker id, so jobs in progress survive the rotation.
            db.execute("UPDATE workers SET token_hash=? WHERE id=?", (digest(token), worker_id))
            db.commit()
        return {"worker_id": worker_id, "worker_token": token,
                "warning": "Worker token shown once. Store securely. Never commit to Git."}

    @app.post("/api/admin/workers/{worker_id}/model", dependencies=[Depends(admin)])
    def retag(worker_id: str, payload: WorkerModelIn):
        """Move a worker to a different operator-approved model, in place.

        Identity, token and credit history are untouched (unlike remove+add) so an operator can
        resize a device's model without reissuing its worker token or losing its trust/credit
        history. The device's own CAG_MODEL setting lives in its local worker.env, not here — the
        operator still has to edit that file on the device and restart it to match.
        """
        approved(payload.model)
        with database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revoked_at FROM workers WHERE id=?", (worker_id,)).fetchone()
            if row is None:
                db.rollback()
                raise HTTPException(404, "Unknown worker")
            if row["revoked_at"]:
                db.rollback()
                raise HTTPException(409, "Worker revoked")
            db.execute("UPDATE workers SET model=? WHERE id=?", (payload.model, worker_id))
            db.commit()
        return {"worker_id": worker_id, "model": payload.model}

    @app.post("/api/admin/workers/{worker_id}/settle", dependencies=[Depends(admin)])
    def settle(worker_id: str, payload: Settlement):
        """Manually approve provisional demo credits after out-of-band verification.

        Approval is a human administrative decision, NOT cryptographic verification.
        """
        timestamp = ts()
        with database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            update = db.execute("UPDATE workers SET provisional_credits=provisional_credits-? WHERE id=? AND provisional_credits>=?",
                                (payload.credits, worker_id, payload.credits))
            if update.rowcount != 1:
                db.rollback()
                raise HTTPException(409, "Unknown worker or insufficient provisional credits")
            owner = db.execute("SELECT COALESCE(owner_id,id) o FROM workers WHERE id=?", (worker_id,)).fetchone()["o"]
            db.execute("UPDATE users SET credits=credits+? WHERE id=?", (payload.credits, owner))
            db.execute("INSERT INTO ledger(actor_type,actor_id,delta,job_id,note,created_at) VALUES ('user',?,?,NULL,?,?)",
                       (owner, payload.credits, "manual_review:" + payload.audit_note, timestamp))
            db.commit()
        return {"settled_demo_credits": payload.credits, "user_id": owner, "verification": "manual_admin_only"}

    @app.get("/api/worker/me")
    def worker_me(w: sqlite3.Row = Depends(worker)):
        """Self-service status for the worker's own device/GUI — same token already used to
        claim/complete jobs, no new credential. Deliberately excludes the linked user account's
        spendable balance: that stays behind the separate user_token boundary (see README's
        worker-token-vs-user-token split), so a compromised worker cannot read spending credits."""
        return {"id": w["id"], "name": w["name"], "model": w["model"], "trust_level": w["trust_level"],
                "provisional_credits": w["provisional_credits"], "last_seen": w["last_seen"],
                "note": "provisional_credits are unverified until an administrator settles them"}

    @app.post("/api/worker/heartbeat")
    def heartbeat(payload: Heartbeat, w: sqlite3.Row = Depends(worker)):
        with database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            reauthorize(db, w)
            # Sample time after acquiring the writer lock; waiting may exhaust a lease.
            current = now()
            timestamp = ts(current)
            db.execute("UPDATE workers SET last_seen=?,capabilities_json=? WHERE id=?",
                       (timestamp, json.dumps(payload.capabilities.model_dump(exclude_none=True)), w["id"]))
            if payload.job_id and payload.lease_token:
                update = db.execute("""UPDATE jobs SET lease_until=?, updated_at=?
                    WHERE id=? AND assigned_worker=? AND lease_hash=? AND status='running' AND lease_until>?""",
                    (ts(current + timedelta(seconds=cfg.lease_seconds)), timestamp, payload.job_id,
                     w["id"], digest(payload.lease_token), timestamp))
                if update.rowcount != 1:
                    db.rollback()
                    raise HTTPException(409, "Lease no longer valid")
            db.commit()
        return {"ok": True}

    @app.post("/api/worker/claim")
    def claim(w: sqlite3.Row = Depends(worker)):
        with database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            reauthorize(db, w)
            current = now()
            timestamp = ts(current)
            sweep(db, current, cfg.queue_timeout_seconds)
            chosen = db.execute("""SELECT * FROM jobs WHERE model=? AND attempts<? AND status='queued'
                AND (privacy='community' OR ?='trusted') ORDER BY created_at LIMIT 1""",
                (w["model"], MAX_ATTEMPTS, w["trust_level"])).fetchone()
            if chosen is None:
                db.commit()
                return {"job": None}
            lease = secrets.token_urlsafe(32)
            db.execute("""UPDATE jobs SET status='running',assigned_worker=?,lease_hash=?,lease_until=?,
                attempts=attempts+1,updated_at=? WHERE id=?""",
                (w["id"], digest(lease), ts(current + timedelta(seconds=cfg.lease_seconds)), timestamp, chosen["id"]))
            db.execute("UPDATE workers SET last_seen=? WHERE id=?", (timestamp, w["id"]))
            db.commit()
        return {"job": {"id": chosen["id"], "model": chosen["model"],
                        "messages": json.loads(chosen["messages_json"]),
                        "max_output_tokens": chosen["max_output_tokens"], "privacy": chosen["privacy"],
                        "lease_token": lease}}

    @app.post("/api/worker/complete")
    def complete(payload: Completion, w: sqlite3.Row = Depends(worker)):
        with database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            reauthorize(db, w)
            timestamp = ts()
            chosen = db.execute("SELECT * FROM jobs WHERE id=?", (payload.job_id,)).fetchone()
            if (not chosen or chosen["status"] != "running" or
                chosen["assigned_worker"] != w["id"] or
                not hmac.compare_digest(chosen["lease_hash"] or "", digest(payload.lease_token)) or
                not chosen["lease_until"] or chosen["lease_until"] <= timestamp):
                db.rollback()
                raise HTTPException(409, "Job lease expired or already completed")
            if payload.ok:
                if not payload.content.strip():
                    db.rollback()
                    raise HTTPException(422, "Empty result")
                db.execute("""UPDATE jobs SET status='completed',result_json=?,error=NULL,
                    lease_hash=NULL,lease_until=NULL,updated_at=? WHERE id=?""",
                    (json.dumps({"content": payload.content, "usage": payload.usage}), timestamp, payload.job_id))
                if cfg.auto_settle:  # operator opted in: spendable at once, still NOT verified
                    owner = w["owner_id"] or w["id"]
                    db.execute("UPDATE users SET credits=credits+? WHERE id=?", (COST, owner))
                    db.execute("INSERT INTO ledger(actor_type,actor_id,delta,job_id,note,created_at) VALUES ('user',?,?,?,'auto_settled_unverified',?)",
                               (owner, COST, payload.job_id, timestamp))
                else:
                    db.execute("UPDATE workers SET provisional_credits=provisional_credits+? WHERE id=?", (COST, w["id"]))
                    db.execute("INSERT INTO ledger(actor_type,actor_id,delta,job_id,note,created_at) VALUES ('worker',?,?,?,'unverified_provisional_reward',?)",
                               (w["id"], COST, payload.job_id, timestamp))
            else:
                requeue_or_fail(db, chosen, payload.error, timestamp, "refund_failed_job")
            db.commit()
        return {"accepted": True, "reward_verification": "NOT_VERIFIED"}

    return app


app = create_app()
