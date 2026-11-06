from datetime import timedelta

import pytest

from coordinator.app.main import MAX_ATTEMPTS, Settings, requeue_or_fail
from grid_helpers import ADMIN, USER, auth, enroll, freeze_clock, submit

T = "2030-01-01T00:00:00.000000+00:00"


def test_lease_duration_comes_from_settings(make_api, monkeypatch):
    clock = freeze_clock(monkeypatch)
    api = make_api(lease_seconds=60)
    token = enroll(api, "w")
    job_id = submit(api).json()["id"]
    api.post("/api/worker/claim", headers=auth(token))
    with api.app.state.database.connect() as db:
        row = db.execute("SELECT lease_until FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["lease_until"] == (clock[0] + timedelta(seconds=60)).isoformat(timespec="microseconds")


@pytest.mark.parametrize("seconds", [0, 4, 3601])
def test_lease_seconds_out_of_range_is_rejected(seconds):
    with pytest.raises(RuntimeError, match="CAG_LEASE_SECONDS"):
        Settings(admin_key=ADMIN, user_key=USER, db_path="unused", lease_seconds=seconds).validate()


def test_requeue_or_fail_refunds_only_on_last_attempt(api):
    token = enroll(api, "w")
    job_id = submit(api).json()["id"]
    api.post("/api/worker/claim", headers=auth(token))
    with api.app.state.database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert requeue_or_fail(db, job, "synthetic", T, "refund_test") == "queued"
        db.commit()
        db.execute("UPDATE jobs SET attempts=?, status='running' WHERE id=?", (MAX_ATTEMPTS, job_id))
        db.execute("BEGIN IMMEDIATE")
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert requeue_or_fail(db, job, "synthetic", T, "refund_test") == "failed"
        db.commit()
        refunds = db.execute("SELECT delta FROM ledger WHERE job_id=? AND note='refund_test'", (job_id,)).fetchall()
    assert [r["delta"] for r in refunds] == [5]
    assert api.get("/api/balance", headers=auth(USER)).json()["demo_credits"] == 100

def test_queued_job_times_out_and_refunds_once(make_api, monkeypatch):
    clock = freeze_clock(monkeypatch)
    api = make_api(queue_timeout_seconds=3600)
    job_id = submit(api).json()["id"]
    clock[0] += timedelta(seconds=3599)
    assert api.get(f"/api/jobs/{job_id}", headers=auth(USER)).json()["status"] == "queued"
    clock[0] += timedelta(seconds=1)
    status = api.get(f"/api/jobs/{job_id}", headers=auth(USER)).json()
    assert status["status"] == "failed" and status["error"] == "Queue timeout: no matching worker"
    api.get(f"/api/jobs/{job_id}", headers=auth(USER))
    assert api.get("/api/balance", headers=auth(USER)).json()["demo_credits"] == 100
    with api.app.state.database.connect() as db:
        rows = db.execute("SELECT delta FROM ledger WHERE job_id=? AND note='refund_queue_timeout'", (job_id,)).fetchall()
    assert [r["delta"] for r in rows] == [5]


def test_timed_out_job_is_not_claimed(make_api, monkeypatch):
    clock = freeze_clock(monkeypatch)
    api = make_api(queue_timeout_seconds=60)
    token = enroll(api, "late")
    submit(api)
    clock[0] += timedelta(seconds=61)
    assert api.post("/api/worker/claim", headers=auth(token)).json()["job"] is None
    assert api.get("/api/balance", headers=auth(USER)).json()["demo_credits"] == 100


@pytest.mark.parametrize("seconds", [59, 604801])
def test_queue_timeout_out_of_range_is_rejected(seconds):
    with pytest.raises(RuntimeError, match="CAG_QUEUE_TIMEOUT_SECONDS"):
        Settings(admin_key=ADMIN, user_key=USER, db_path="unused", queue_timeout_seconds=seconds).validate()


def test_requeued_job_gets_a_fresh_queue_timeout(make_api, monkeypatch):
    """Running time must not count: a requeued job keeps its remaining attempts."""
    clock = freeze_clock(monkeypatch)
    api = make_api(queue_timeout_seconds=60, lease_seconds=180)
    token = enroll(api, "crashes")
    job_id = submit(api).json()["id"]
    api.post("/api/worker/claim", headers=auth(token))
    clock[0] += timedelta(seconds=181)  # worker died; lease expired long after the queue timeout
    status = api.get(f"/api/jobs/{job_id}", headers=auth(USER)).json()
    assert status["status"] == "queued" and status["attempts"] == 1
    clock[0] += timedelta(seconds=60)
    assert api.get(f"/api/jobs/{job_id}", headers=auth(USER)).json()["status"] == "failed"
    assert api.get("/api/balance", headers=auth(USER)).json()["demo_credits"] == 100
