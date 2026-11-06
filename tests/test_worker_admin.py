import pytest

from grid_helpers import ADMIN, USER, auth, enroll_full, submit


def revoke(api, worker_id, token=ADMIN):
    return api.post(f"/api/admin/workers/{worker_id}/revoke", headers=auth(token))


def test_revoked_worker_token_is_rejected(api):
    w = enroll_full(api, "gone")
    r = revoke(api, w["id"])
    assert r.status_code == 200
    assert r.json() == {"worker_id": w["id"], "revoked": True, "requeued_jobs": 0, "failed_jobs": 0}
    assert api.post("/api/worker/claim", headers=auth(w["worker_token"])).status_code == 403
    assert api.post("/api/worker/heartbeat", headers=auth(w["worker_token"]), json={}).status_code == 403
    assert api.get("/api/admin/workers", headers=auth(ADMIN)).json()[0]["revoked_at"] is not None


def test_revoke_requeues_running_job_for_another_worker(api):
    old, new = enroll_full(api, "a-old"), enroll_full(api, "b-new")
    job_id = submit(api).json()["id"]
    job = api.post("/api/worker/claim", headers=auth(old["worker_token"])).json()["job"]
    assert job["id"] == job_id
    assert revoke(api, old["id"]).json()["requeued_jobs"] == 1
    assert api.post("/api/worker/claim", headers=auth(new["worker_token"])).json()["job"]["id"] == job_id
    assert api.post("/api/worker/complete", headers=auth(old["worker_token"]), json={
        "job_id": job_id, "lease_token": job["lease_token"], "ok": True, "content": "late"}).status_code == 403


def test_revoke_on_last_attempt_fails_job_and_refunds(api):
    w = enroll_full(api, "last")
    job_id = submit(api).json()["id"]
    api.post("/api/worker/claim", headers=auth(w["worker_token"]))
    with api.app.state.database.connect() as db:
        db.execute("UPDATE jobs SET attempts=3 WHERE id=?", (job_id,))
    assert revoke(api, w["id"]).json()["failed_jobs"] == 1
    status = api.get(f"/api/jobs/{job_id}", headers=auth(USER)).json()
    assert status["status"] == "failed" and status["error"] == "Worker revoked"
    assert api.get("/api/balance", headers=auth(USER)).json()["demo_credits"] == 100


def test_revoke_errors_and_admin_only(api):
    w = enroll_full(api, "x")
    assert revoke(api, "no-such-worker").status_code == 404
    assert revoke(api, w["id"], token=USER).status_code == 403
    assert revoke(api, w["id"]).status_code == 200
    assert revoke(api, w["id"]).status_code == 409


def test_revoked_workers_user_token_keeps_balance_access(api):
    w = enroll_full(api, "owner")
    revoke(api, w["id"])
    assert api.get("/api/balance", headers=auth(w["user_token"])).status_code == 200


def test_rotate_replaces_token_and_keeps_running_lease(api):
    w = enroll_full(api, "rot")
    job_id = submit(api).json()["id"]
    job = api.post("/api/worker/claim", headers=auth(w["worker_token"])).json()["job"]
    r = api.post(f"/api/admin/workers/{w['id']}/rotate", headers=auth(ADMIN))
    assert r.status_code == 200
    new_token = r.json()["worker_token"]
    assert new_token.startswith("cag_") and new_token != w["worker_token"]
    assert api.post("/api/worker/claim", headers=auth(w["worker_token"])).status_code == 403
    assert api.post("/api/worker/complete", headers=auth(new_token), json={
        "job_id": job_id, "lease_token": job["lease_token"], "ok": True, "content": "done"}).status_code == 200


def test_rotate_rejects_unknown_and_revoked(api):
    w = enroll_full(api, "rot2")
    assert api.post("/api/admin/workers/nope/rotate", headers=auth(ADMIN)).status_code == 404
    revoke(api, w["id"])
    assert api.post(f"/api/admin/workers/{w['id']}/rotate", headers=auth(ADMIN)).status_code == 409


def test_heartbeat_stores_only_known_capability_fields(api):
    w = enroll_full(api, "meta")
    r = api.post("/api/worker/heartbeat", headers=auth(w["worker_token"]), json={"capabilities": {
        "hostname": "private-name", "cpu_logical": 8, "local_implementation": "ollama", "extra": "x"}})
    assert r.status_code == 200
    caps = api.get("/api/admin/workers", headers=auth(ADMIN)).json()[0]["capabilities"]
    assert caps == {"cpu_logical": 8, "local_implementation": "ollama"}


@pytest.mark.parametrize("change", ["revoke", "rotate"])
def test_token_change_committed_while_claim_waits_for_lock_is_enforced(api, monkeypatch, change):
    """A claim authenticated just before revoke/rotate commits must not get a job afterwards."""
    import sqlite3
    from contextlib import closing, contextmanager

    w = enroll_full(api, "racing")
    job_id = submit(api).json()["id"]
    database = api.app.state.database
    connect = database.connect
    fired = []

    def change_token(sql):
        if sql.startswith("BEGIN IMMEDIATE") and not fired:
            fired.append(sql)
            with closing(sqlite3.connect(database.path)) as other:
                column = "revoked_at" if change == "revoke" else "token_hash"
                other.execute(f"UPDATE workers SET {column}='changed-meanwhile' WHERE id=?", (w["id"],))
                other.commit()

    @contextmanager
    def observed_connect():
        with connect() as db:
            db.set_trace_callback(change_token)
            yield db

    monkeypatch.setattr(database, "connect", observed_connect)
    assert api.post("/api/worker/claim", headers=auth(w["worker_token"])).status_code == 403
    monkeypatch.setattr(database, "connect", connect)
    with connect() as db:
        assert db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"] == "queued"
