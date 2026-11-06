import sqlite3
from contextlib import closing

from coordinator.app.database import Database

OLD_WORKERS = """CREATE TABLE workers (id TEXT PRIMARY KEY, name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE, model TEXT NOT NULL,
    trust_level TEXT NOT NULL CHECK(trust_level IN ('trusted','community')),
    provisional_credits INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT, capabilities_json TEXT NOT NULL DEFAULT '{}')"""
KEY = "synthetic-demo-key-long-enough-0001"


def test_initialize_adds_revoked_at_to_existing_database(tmp_path):
    path = str(tmp_path / "old.sqlite3")
    with closing(sqlite3.connect(path)) as db:
        db.execute(OLD_WORKERS)
        db.execute("INSERT INTO workers(id,name,token_hash,model,trust_level) "
                   "VALUES ('w1','old','h','demo-model','trusted')")
        db.commit()
    Database(path).initialize(KEY, 100)
    Database(path).initialize(KEY, 100)  # restart must be idempotent
    with closing(sqlite3.connect(path)) as db:
        columns = [r[1] for r in db.execute("PRAGMA table_info(workers)")]
        rows = db.execute("SELECT name, revoked_at FROM workers").fetchall()
    assert columns.count("revoked_at") == 1
    assert rows == [("old", None)]
