"""Shared test helpers; synthetic credentials only."""
from datetime import datetime, timezone, timedelta

from fastapi.testclient import TestClient

ADMIN = "test-admin-only-secret-long-enough-00001"
USER = "test-user-only-secret-long-enough-000002"

def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}

def enroll(api: TestClient, name: str, trust: str = "trusted", model: str = "demo-model") -> str:
    r = api.post("/api/admin/workers", headers=auth(ADMIN), json={"name": name, "trust_level": trust, "model": model})
    assert r.status_code == 201, r.text
    return r.json()["worker_token"]

def enroll_full(api: TestClient, name: str, trust: str = "trusted", model: str = "demo-model") -> dict:
    r = api.post("/api/admin/workers", headers=auth(ADMIN), json={"name": name, "trust_level": trust, "model": model})
    assert r.status_code == 201, r.text
    return r.json()

def submit(api: TestClient, privacy: str = "trusted", allow_untrusted: bool = False, model: str = "demo-model"):
    return api.post("/api/jobs", headers=auth(USER), json={"model": model,
       "messages": [{"role": "user", "content": "Hello"}], "privacy": privacy,
       "allow_untrusted": allow_untrusted})

def expire_lease(api: TestClient, job_id: str) -> None:
    """Simulate a crashed worker whose lease ran out."""
    with api.app.state.database.connect() as db:
        db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (
            (datetime.now(timezone.utc)-timedelta(seconds=5)).isoformat(timespec="microseconds"), job_id))

def freeze_clock(monkeypatch, start: datetime = datetime(2030, 1, 1, tzinfo=timezone.utc)) -> list[datetime]:
    import coordinator.app.main as coordinator
    clock = [start]
    monkeypatch.setattr(coordinator, "now", lambda: clock[0])
    return clock
