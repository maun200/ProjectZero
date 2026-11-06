from concurrent.futures import ThreadPoolExecutor

from grid_helpers import ADMIN, USER, auth, enroll, enroll_full, submit


def cancel(api, job_id, token=USER):
    return api.post(f"/api/jobs/{job_id}/cancel", headers=auth(token))


def balance(api):
    return api.get("/api/balance", headers=auth(USER)).json()["demo_credits"]


def test_cancel_queued_job_refunds_and_is_never_claimed(api):
    token = enroll(api, "w")
    job_id = submit(api).json()["id"]
    r = cancel(api, job_id)
    assert r.status_code == 200 and r.json() == {"id": job_id, "status": "cancelled", "refunded": 5}
    assert balance(api) == 100
    assert api.post("/api/worker/claim", headers=auth(token)).json()["job"] is None
    status = api.get(f"/api/jobs/{job_id}", headers=auth(USER)).json()
    assert status["status"] == "cancelled" and status["error"] == "Cancelled by user"


def test_cancel_running_job_invalidates_lease_without_reward(api):
    token = enroll(api, "w")
    job_id = submit(api).json()["id"]
    job = api.post("/api/worker/claim", headers=auth(token)).json()["job"]
    assert cancel(api, job_id).status_code == 200
    lease = {"job_id": job_id, "lease_token": job["lease_token"]}
    assert api.post("/api/worker/heartbeat", headers=auth(token), json=lease).status_code == 409
    assert api.post("/api/worker/complete", headers=auth(token),
                    json={**lease, "ok": True, "content": "late"}).status_code == 409
    assert api.get("/api/admin/workers", headers=auth(ADMIN)).json()[0]["provisional_credits"] == 0
    assert balance(api) == 100


def test_cancel_terminal_or_foreign_job_is_rejected(api):
    other = enroll_full(api, "other-user")
    job_id = submit(api).json()["id"]
    assert cancel(api, job_id, token=other["user_token"]).status_code == 404
    assert cancel(api, "missing").status_code == 404
    assert cancel(api, job_id).status_code == 200
    assert cancel(api, job_id).status_code == 409


def test_concurrent_cancel_refunds_once(clients):
    job_id = submit(clients[0]).json()["id"]
    with ThreadPoolExecutor(len(clients)) as pool:
        codes = list(pool.map(lambda c: cancel(c, job_id).status_code, clients))
    assert sorted(codes) == [200] + [409] * 5
    assert balance(clients[0]) == 100


def test_cancel_racing_completion_settles_exactly_once(clients):
    token = enroll(clients[0], "racer")
    job_id = submit(clients[0]).json()["id"]
    job = clients[0].post("/api/worker/claim", headers=auth(token)).json()["job"]
    done = {"job_id": job_id, "lease_token": job["lease_token"], "ok": True, "content": "done"}
    with ThreadPoolExecutor(2) as pool:
        finished = pool.submit(clients[1].post, "/api/worker/complete", headers=auth(token), json=done)
        cancelled = pool.submit(cancel, clients[2], job_id)
        codes = sorted([finished.result().status_code, cancelled.result().status_code])
    assert codes == [200, 409]
    state = clients[0].get(f"/api/jobs/{job_id}", headers=auth(USER)).json()["status"]
    provisional = clients[0].get("/api/admin/workers", headers=auth(ADMIN)).json()[0]["provisional_credits"]
    assert (state, balance(clients[0]), provisional) in {("completed", 95, 5), ("cancelled", 100, 0)}
