"""Members, machines, invites, online list and chat channels (v0.3).

Everything here is plaintext, stored in the coordinator database and readable by the operator.
The UI says so; never treat these chats as private messaging.
"""
import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .database import Database, digest
from .ratelimit import RateLimiter

NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{2,23}$"
RESERVED = {"admin", "administrator", "operator", "system", "demo", "lobby", "moderator", "root",
            "support", "projectzero", "everyone", "here"}
INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I confusion
ONLINE_SECONDS = 90
ACTIVE_SECONDS = 300
KEEP_MESSAGES = 1000
LOBBY = "lobby"


def new_invite_code() -> str:
    raw = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def normalize_code(code: str) -> str:
    return re.sub(r"[\s-]", "", code).upper()


def consume_invite(db: sqlite3.Connection, code: str, timestamp: str) -> None:
    """Use up one slot of an invite. Must run inside an open write transaction."""
    used = db.execute("UPDATE invites SET uses_left=uses_left-1 WHERE code_hash=? AND uses_left>0 AND expires_at>?",
                      (digest(normalize_code(code)), timestamp))
    if used.rowcount != 1:
        db.rollback()
        raise HTTPException(403, "Invalid, used up or expired invite code")


def direct_channel_id(a: str, b: str) -> str:
    return "dm-" + hashlib.sha256("\n".join(sorted((a, b))).encode()).hexdigest()[:24]


class RegisterIn(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)
    invite_code: str | None = Field(default=None, max_length=40)


class RenameIn(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)


class MachineIn(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$")
    model: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")


class InviteIn(BaseModel):
    ttl_seconds: int = Field(default=86400, ge=60, le=30 * 86400)
    uses: int = Field(default=1, ge=1, le=1000)


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    members: list[str] = Field(default_factory=list, max_length=49)


class MemberIn(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)


class PostIn(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


@dataclass
class Context:
    """What the community routes need from the core app."""
    database: Database
    throttle: Callable[[str], None]
    approved: Callable[[str], None]
    admin: Callable
    now: Callable[[], datetime]
    ts: Callable[..., str]
    revoke_worker: Callable[[sqlite3.Connection, str, str], dict]
    registration: str
    signup_credits: int
    auto_settle: bool
    register_per_hour: int
    max_machines: int
    cost: int


def install(app: FastAPI, ctx: Context) -> None:
    per_source = RateLimiter(ctx.register_per_hour, window=3600)
    overall = RateLimiter(ctx.register_per_hour * 10, window=3600)

    def check_name_free(name: str) -> None:
        if name.lower() in RESERVED:
            raise HTTPException(409, "This name is reserved")

    def member(authorization: str | None = Header(default=None)) -> sqlite3.Row:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer token required")
        with ctx.database.connect() as db:
            row = db.execute("SELECT id,name,credits,last_active,created_at FROM users WHERE api_key_hash=?",
                             (digest(authorization[7:]),)).fetchone()
            if row is None:
                raise HTTPException(403, "Invalid user token")
            ctx.throttle("user:" + row["id"])
            current = ctx.now()
            if not row["last_active"] or row["last_active"] < ctx.ts(current - timedelta(seconds=30)):
                db.execute("UPDATE users SET last_active=? WHERE id=?", (ctx.ts(current), row["id"]))
        return row

    def registered(row: sqlite3.Row = Depends(member)) -> sqlite3.Row:
        """Only self-registered member accounts (created via /api/register) get member self-service.

        The shared demo key and the user rows paired with admin/self-enrolled workers stay
        limited to jobs and balance; they must not rename themselves, add machines or chat.
        """
        if not row["created_at"]:
            raise HTTPException(403, "Only registered member accounts can do this")
        return row

    def named(row: sqlite3.Row = Depends(registered)) -> sqlite3.Row:
        if not row["name"]:
            raise HTTPException(403, "Choose a display name first (PATCH /api/me)")
        return row

    def user_by_name(db: sqlite3.Connection, name: str) -> sqlite3.Row:
        found = db.execute("SELECT id,name FROM users WHERE name=? COLLATE NOCASE", (name,)).fetchone()
        if found is None:
            raise HTTPException(404, f"Unknown user: {name}")
        return found

    def require_channel(db: sqlite3.Connection, channel_id: str, user_id: str) -> sqlite3.Row:
        channel = db.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
        if channel is None:
            raise HTTPException(404, "Unknown channel")
        if channel["kind"] != "lobby" and db.execute(
                "SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?", (channel_id, user_id)).fetchone() is None:
            raise HTTPException(404, "Unknown channel")  # do not reveal channels you are not in
        return channel

    # ---------------------------------------------------------------- registration
    @app.post("/api/register", status_code=201)
    def register(payload: RegisterIn, request: Request):
        if ctx.registration == "off":
            raise HTTPException(404, "Self-registration is disabled on this coordinator")
        source = request.client.host if request.client else "unknown"
        for limiter, key in ((per_source, "src:" + source), (overall, "all")):
            wait = limiter.check(key)
            if wait > 0:
                raise HTTPException(429, "Too many registrations, try again later",
                                    headers={"Retry-After": str(int(wait) + 1)})
        check_name_free(payload.name)
        ident, token, timestamp = str(uuid.uuid4()), "cag_usr_" + secrets.token_urlsafe(32), ctx.ts()
        with ctx.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if ctx.registration == "invite":
                if not payload.invite_code:
                    db.rollback()
                    raise HTTPException(403, "This coordinator requires an invite code")
                consume_invite(db, payload.invite_code, timestamp)
            try:
                db.execute("INSERT INTO users(id,api_key_hash,credits,name,created_at,last_active) VALUES (?,?,?,?,?,?)",
                           (ident, digest(token), ctx.signup_credits, payload.name, timestamp, timestamp))
            except sqlite3.IntegrityError:
                db.rollback()
                raise HTTPException(409, "This name is already taken") from None
            if ctx.signup_credits:
                db.execute("INSERT INTO ledger(actor_type,actor_id,delta,job_id,note,created_at) VALUES ('user',?,?,NULL,'signup_credits',?)",
                           (ident, ctx.signup_credits, timestamp))
            db.commit()
        return {"id": ident, "name": payload.name, "user_token": token, "credits": ctx.signup_credits,
                "warning": "The user token is shown once. It is your login: keep it private."}

    @app.post("/api/admin/invites", dependencies=[Depends(ctx.admin)])
    def create_invite(payload: InviteIn):
        code, current = new_invite_code(), ctx.now()
        expires = ctx.ts(current + timedelta(seconds=payload.ttl_seconds))
        with ctx.database.connect() as db:
            db.execute("INSERT INTO invites(code_hash,expires_at,uses_left,created_at) VALUES (?,?,?,?)",
                       (digest(normalize_code(code)), expires, payload.uses, ctx.ts(current)))
        return {"code": code, "expires_at": expires, "uses": payload.uses}

    # ---------------------------------------------------------------- me
    def machines_of(db: sqlite3.Connection, user_id: str) -> list[dict]:
        cutoff = ctx.ts(ctx.now() - timedelta(seconds=ONLINE_SECONDS))
        rows = db.execute("""SELECT id,name,model,trust_level,provisional_credits,last_seen FROM workers
            WHERE (owner_id=? OR id=?) AND revoked_at IS NULL ORDER BY name""", (user_id, user_id)).fetchall()
        return [{**dict(r), "online": bool(r["last_seen"] and r["last_seen"] > cutoff)} for r in rows]

    @app.get("/api/me")
    def me(row: sqlite3.Row = Depends(member)):
        with ctx.database.connect() as db:
            machines = machines_of(db, row["id"])
            earned = db.execute("""SELECT COALESCE(SUM(l.delta),0) s FROM ledger l
                WHERE l.actor_id=? AND l.note='auto_settled_unverified'""", (row["id"],)).fetchone()["s"]
        return {"id": row["id"], "name": row["name"], "credits": row["credits"],
                "provisional_credits": sum(m["provisional_credits"] for m in machines),
                "earned_credits": earned, "machines": machines, "job_cost": ctx.cost,
                "auto_settle": ctx.auto_settle, "registration": ctx.registration}

    @app.patch("/api/me")
    def rename(payload: RenameIn, row: sqlite3.Row = Depends(registered)):
        check_name_free(payload.name)
        with ctx.database.connect() as db:
            try:
                db.execute("UPDATE users SET name=? WHERE id=?", (payload.name, row["id"]))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "This name is already taken") from None
        return {"name": payload.name}

    @app.post("/api/me/rotate-token")
    def rotate_user_token(row: sqlite3.Row = Depends(registered)):
        token = "cag_usr_" + secrets.token_urlsafe(32)
        with ctx.database.connect() as db:
            db.execute("UPDATE users SET api_key_hash=? WHERE id=?", (digest(token), row["id"]))
        return {"user_token": token, "warning": "The old token stopped working. Store the new one now."}

    @app.post("/api/me/machines", status_code=201)
    def add_machine(payload: MachineIn, row: sqlite3.Row = Depends(named)):
        ctx.approved(payload.model)
        ident, token = str(uuid.uuid4()), "cag_" + secrets.token_urlsafe(32)
        with ctx.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) c FROM workers WHERE owner_id=? AND revoked_at IS NULL",
                               (row["id"],)).fetchone()["c"]
            if count >= ctx.max_machines:
                db.rollback()
                raise HTTPException(409, f"At most {ctx.max_machines} active machines per member")
            db.execute("INSERT INTO workers(id,name,token_hash,model,trust_level,owner_id) VALUES (?,?,?,?,'community',?)",
                       (ident, payload.name, digest(token), payload.model, row["id"]))
            db.commit()
        return {"id": ident, "name": payload.name, "model": payload.model, "trust_level": "community",
                "worker_token": token, "warning": "The machine token is shown once. Keep it on that machine only."}

    @app.delete("/api/me/machines/{machine_id}")
    def remove_machine(machine_id: str, row: sqlite3.Row = Depends(member)):
        with ctx.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owned = db.execute("SELECT 1 FROM workers WHERE id=? AND owner_id=? AND revoked_at IS NULL",
                               (machine_id, row["id"])).fetchone()
            if owned is None:
                db.rollback()
                raise HTTPException(404, "Unknown machine")
            result = ctx.revoke_worker(db, machine_id, ctx.ts())
            db.commit()
        return result

    # ---------------------------------------------------------------- online
    @app.get("/api/online")
    def online(row: sqlite3.Row = Depends(member)):
        current = ctx.now()
        with ctx.database.connect() as db:
            machines = db.execute("""SELECT w.name,w.model,w.trust_level,w.capabilities_json,u.name owner
                FROM workers w LEFT JOIN users u ON u.id=COALESCE(w.owner_id,w.id)
                WHERE w.revoked_at IS NULL AND w.last_seen>? ORDER BY w.name""",
                (ctx.ts(current - timedelta(seconds=ONLINE_SECONDS)),)).fetchall()
            people = db.execute("SELECT name FROM users WHERE name IS NOT NULL AND last_active>? ORDER BY name COLLATE NOCASE",
                                (ctx.ts(current - timedelta(seconds=ACTIVE_SECONDS)),)).fetchall()
        result = []
        for m in machines:
            caps = json.loads(m["capabilities_json"] or "{}")
            result.append({"name": m["name"], "model": m["model"], "trust_level": m["trust_level"],
                           "owner": m["owner"], "platform": caps.get("platform"),
                           "cpu_logical": caps.get("cpu_logical"), "gpu": bool(caps.get("nvidia_smi_available"))})
        return {"machines": result, "people": [p["name"] for p in people],
                "models": sorted({m["model"] for m in result})}

    # ---------------------------------------------------------------- channels
    @app.get("/api/channels")
    def channels(row: sqlite3.Row = Depends(named)):
        with ctx.database.connect() as db:
            rows = db.execute("""SELECT c.*, (SELECT MAX(id) FROM messages m WHERE m.channel_id=c.id) last_id
                FROM channels c WHERE c.kind='lobby' OR c.id IN (SELECT channel_id FROM channel_members WHERE user_id=?)
                ORDER BY c.kind='lobby' DESC, c.kind, c.name""", (row["id"],)).fetchall()
            out = []
            for c in rows:
                names = [r["name"] for r in db.execute("""SELECT u.name FROM channel_members cm JOIN users u ON u.id=cm.user_id
                    WHERE cm.channel_id=? ORDER BY u.name COLLATE NOCASE""", (c["id"],))] if c["kind"] != "lobby" else []
                title = c["name"]
                if c["kind"] == "direct":
                    title = next((n for n in names if n != row["name"]), row["name"])
                out.append({"id": c["id"], "kind": c["kind"], "name": title, "members": names,
                            "last_message_id": c["last_id"] or 0})
        return out

    @app.post("/api/channels", status_code=201)
    def create_group(payload: GroupIn, row: sqlite3.Row = Depends(named)):
        ident, timestamp = "grp-" + uuid.uuid4().hex[:20], ctx.ts()
        with ctx.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                members = {user_by_name(db, n)["id"] for n in payload.members} | {row["id"]}
            except HTTPException:
                db.rollback()
                raise
            db.execute("INSERT INTO channels(id,kind,name,created_by,created_at) VALUES (?,'group',?,?,?)",
                       (ident, payload.name.strip() or "Gruppe", row["id"], timestamp))
            db.executemany("INSERT INTO channel_members(channel_id,user_id) VALUES (?,?)", [(ident, m) for m in members])
            db.commit()
        return {"id": ident, "kind": "group", "name": payload.name}

    @app.post("/api/channels/{channel_id}/members")
    def add_member(channel_id: str, payload: MemberIn, row: sqlite3.Row = Depends(named)):
        with ctx.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                channel = require_channel(db, channel_id, row["id"])
                if channel["kind"] != "group":
                    raise HTTPException(409, "Only groups can get new members")
                other = user_by_name(db, payload.name)
                if db.execute("SELECT COUNT(*) c FROM channel_members WHERE channel_id=?", (channel_id,)).fetchone()["c"] >= 50:
                    raise HTTPException(409, "Groups are limited to 50 members")
            except HTTPException:
                db.rollback()
                raise
            db.execute("INSERT OR IGNORE INTO channel_members(channel_id,user_id) VALUES (?,?)", (channel_id, other["id"]))
            db.commit()
        return {"added": other["name"]}

    @app.post("/api/channels/{channel_id}/leave")
    def leave(channel_id: str, row: sqlite3.Row = Depends(named)):
        with ctx.database.connect() as db:
            channel = require_channel(db, channel_id, row["id"])
            if channel["kind"] != "group":
                raise HTTPException(409, "Only groups can be left")
            db.execute("DELETE FROM channel_members WHERE channel_id=? AND user_id=?", (channel_id, row["id"]))
        return {"left": channel_id}

    @app.post("/api/direct", status_code=201)
    def direct(payload: MemberIn, row: sqlite3.Row = Depends(named)):
        with ctx.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                other = user_by_name(db, payload.name)
            except HTTPException:
                db.rollback()
                raise
            ident = direct_channel_id(row["id"], other["id"])
            db.execute("INSERT OR IGNORE INTO channels(id,kind,name,created_by,created_at) VALUES (?,'direct','direct',?,?)",
                       (ident, row["id"], ctx.ts()))
            db.executemany("INSERT OR IGNORE INTO channel_members(channel_id,user_id) VALUES (?,?)",
                           [(ident, row["id"]), (ident, other["id"])])
            db.commit()
        return {"id": ident, "kind": "direct", "name": other["name"]}

    @app.get("/api/channels/{channel_id}/messages")
    def read_messages(channel_id: str, after: int = 0, limit: int = 50, row: sqlite3.Row = Depends(named)):
        limit = max(1, min(limit, 200))
        with ctx.database.connect() as db:
            require_channel(db, channel_id, row["id"])
            query = """SELECT m.id,u.name author,m.body,m.created_at FROM messages m JOIN users u ON u.id=m.user_id
                WHERE m.channel_id=? AND m.id>? ORDER BY m.id {order} LIMIT ?"""
            if after > 0:
                rows = db.execute(query.format(order="ASC"), (channel_id, after, limit)).fetchall()
            else:  # newest page, returned oldest first
                rows = db.execute(query.format(order="DESC"), (channel_id, 0, limit)).fetchall()[::-1]
        return [dict(r) for r in rows]

    @app.post("/api/channels/{channel_id}/messages", status_code=201)
    def post_message(channel_id: str, payload: PostIn, row: sqlite3.Row = Depends(named)):
        body = payload.body.strip()
        if not body:
            raise HTTPException(422, "Empty message")
        with ctx.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                require_channel(db, channel_id, row["id"])
            except HTTPException:
                db.rollback()
                raise
            cursor = db.execute("INSERT INTO messages(channel_id,user_id,body,created_at) VALUES (?,?,?,?)",
                                (channel_id, row["id"], body, ctx.ts()))
            db.execute("""DELETE FROM messages WHERE channel_id=? AND id<=(
                SELECT id FROM messages WHERE channel_id=? ORDER BY id DESC LIMIT 1 OFFSET ?)""",
                (channel_id, channel_id, KEEP_MESSAGES))
            db.commit()
        return {"id": cursor.lastrowid}

    # ---------------------------------------------------------------- moderation
    @app.delete("/api/admin/messages/{message_id}", dependencies=[Depends(ctx.admin)])
    def delete_message(message_id: int):
        with ctx.database.connect() as db:
            if db.execute("DELETE FROM messages WHERE id=?", (message_id,)).rowcount != 1:
                raise HTTPException(404, "Unknown message")
        return {"deleted": message_id}

    @app.post("/api/admin/users/{name}/block", dependencies=[Depends(ctx.admin)])
    def block_user(name: str):
        """Invalidate a member's login and revoke all machines they own."""
        with ctx.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                found = user_by_name(db, name)
            except HTTPException:
                db.rollback()
                raise
            db.execute("UPDATE users SET api_key_hash=? WHERE id=?", ("blocked:" + uuid.uuid4().hex, found["id"]))
            timestamp = ctx.ts()
            # Includes a worker paired 1:1 with this user row (admin/self-enrolled: id == user id).
            owned = db.execute("SELECT id FROM workers WHERE (owner_id=? OR id=?) AND revoked_at IS NULL",
                               (found["id"], found["id"])).fetchall()
            for machine in owned:
                ctx.revoke_worker(db, machine["id"], timestamp)
            db.commit()
        return {"blocked": found["name"], "revoked_machines": len(owned)}
