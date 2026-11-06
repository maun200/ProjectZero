import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from coordinator.app.main import Settings, sweep_once
from grid_helpers import ADMIN, USER, auth, enroll, expire_lease, submit


def job_row(api, job_id):  # read the DB directly: GET /api/jobs would sweep by itself
    with api.app.state.database.connect() as db:
        return db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def claimed_and_expired(api):
    token = enroll(api, "crashed")
    job_id = submit(api).json()["id"]
    api.post("/api/worker/claim", headers=auth(token))
    expire_lease(api, job_id)
    return job_id


def test_sweep_once_requeues_expired_lease_without_any_request(api):
    job_id = claimed_and_expired(api)
    sweep_once(api.app.state.database, 3600)
    assert job_row(api, job_id)["status"] == "queued"


def test_background_sweeper_runs_inside_app(make_api):
    api = make_api(sweep_seconds=0.2)
    job_id = claimed_and_expired(api)
    deadline = time.monotonic() + 5
    while job_row(api, job_id)["status"] != "queued":
        assert time.monotonic() < deadline, "sweeper did not run"
        time.sleep(0.1)


def test_sweeper_and_status_polls_refund_only_once(clients):
    job_id = claimed_and_expired(clients[0])
    database = clients[0].app.state.database
    with database.connect() as db:
        db.execute("UPDATE jobs SET attempts=3 WHERE id=?", (job_id,))
    def poke(i):
        if i % 2:
            return sweep_once(database, 3600)
        return clients[i].get(f"/api/jobs/{job_id}", headers=auth(USER)).status_code
    with ThreadPoolExecutor(6) as pool:
        list(pool.map(poke, range(6)))
    with database.connect() as db:
        refunds = db.execute("SELECT delta FROM ledger WHERE job_id=? AND note='refund_expired_job'", (job_id,)).fetchall()
    assert [r["delta"] for r in refunds] == [5]
    assert clients[0].get("/api/balance", headers=auth(USER)).json()["demo_credits"] == 100


@pytest.mark.parametrize("seconds", [-1, 0.1, 3601])
def test_sweep_interval_out_of_range_is_rejected(seconds):
    with pytest.raises(RuntimeError, match="CAG_SWEEP_SECONDS"):
        Settings(admin_key=ADMIN, user_key=USER, db_path="unused", sweep_seconds=seconds).validate()
