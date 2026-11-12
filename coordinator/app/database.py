"""Simple transactional SQLite store for an isolated, single-process prototype.

Not for a public decentralized network: see docs/SECURITY.md.
"""
import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def add_column_if_missing(db: sqlite3.Connection, table: str, column: str, ddl_type: str) -> None:
    """Additive schema migration for databases created by older versions.

    Names are fixed in code and never come from user input.
    """
    if column not in [row[1] for row in db.execute(f"PRAGMA table_info({table})")]:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


class Database:
    def __init__(self, path: str):
        self.path = path

    @contextmanager
    def connect(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self, demo_key: str, initial_credits: int):
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, api_key_hash TEXT NOT NULL,
                credits INTEGER NOT NULL CHECK(credits >= 0)
            );
            CREATE TABLE IF NOT EXISTS workers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE, model TEXT NOT NULL,
                trust_level TEXT NOT NULL CHECK(trust_level IN ('trusted','community')),
                provisional_credits INTEGER NOT NULL DEFAULT 0,
                last_seen TEXT, capabilities_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                model TEXT NOT NULL, messages_json TEXT NOT NULL,
                privacy TEXT NOT NULL CHECK(privacy IN ('trusted','community')),
                max_output_tokens INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                assigned_worker TEXT, lease_hash TEXT, lease_until TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                reservation INTEGER NOT NULL,
                result_json TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_type TEXT NOT NULL, actor_id TEXT NOT NULL,
                delta INTEGER NOT NULL, job_id TEXT,
                note TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS jobs_queue_idx ON jobs(status, model, created_at);
            CREATE TABLE IF NOT EXISTS enroll_codes (
                code_hash TEXT PRIMARY KEY,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL
            );
            """)
            add_column_if_missing(db, "workers", "revoked_at", "TEXT")
            # v0.3: self-registered members, owned machines, invites and chat.
            add_column_if_missing(db, "users", "name", "TEXT")
            add_column_if_missing(db, "users", "created_at", "TEXT")
            add_column_if_missing(db, "users", "last_active", "TEXT")
            add_column_if_missing(db, "workers", "owner_id", "TEXT")
            # v0.4: per-job lifecycle log, so a requester can see what's happening with their request.
            add_column_if_missing(db, "jobs", "events_json", "TEXT NOT NULL DEFAULT '[]'")
            db.executescript("""
            CREATE UNIQUE INDEX IF NOT EXISTS users_name_idx ON users(name COLLATE NOCASE) WHERE name IS NOT NULL;
            CREATE TABLE IF NOT EXISTS invites (
                code_hash TEXT PRIMARY KEY, expires_at TEXT NOT NULL,
                uses_left INTEGER NOT NULL CHECK(uses_left >= 0), created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS channels (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('lobby','group','direct')),
                name TEXT NOT NULL, created_by TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS channel_members (
                channel_id TEXT NOT NULL REFERENCES channels(id), user_id TEXT NOT NULL REFERENCES users(id),
                PRIMARY KEY (channel_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT NOT NULL REFERENCES channels(id),
                user_id TEXT NOT NULL REFERENCES users(id), body TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_channel_idx ON messages(channel_id, id);
            CREATE INDEX IF NOT EXISTS members_user_idx ON channel_members(user_id);
            CREATE INDEX IF NOT EXISTS workers_owner_idx ON workers(owner_id);
            INSERT OR IGNORE INTO channels(id,kind,name,created_by,created_at)
                VALUES ('lobby','lobby','Lobby',NULL,'1970-01-01T00:00:00+00:00');
            """)
            db.execute("""INSERT INTO users(id,api_key_hash,credits) VALUES ('demo',?,?)
                ON CONFLICT(id) DO UPDATE SET api_key_hash=excluded.api_key_hash""",
                (digest(demo_key), initial_credits))
