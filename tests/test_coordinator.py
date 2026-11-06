from datetime import datetime, timedelta, timezone

import pytest
from grid_helpers import ADMIN, USER, auth, enroll, expire_lease, submit

from coordinator.app.database import digest

def test_end_to_end_and_replay_prevention(api):
    token = enroll(api, "desktop")
    response = submit(api)
    assert response.status_code == 201, response.text
    job_id = response.json()["id"]
    assert api.get("/api/balance", headers=auth(USER)).json()["demo_credits"] == 95
    claim = api.post("/api/worker/claim", headers=auth(token)).json()["job"]
    assert claim["id"] == job_id
    finished = {"job_id": job_id, "lease_token": claim["lease_token"], "ok": True, "content": "Hello back"}
    assert api.post("/api/worker/complete", headers=auth(token), json=finished).status_code == 200
    assert api.post("/api/worker/complete", headers=auth(token), json=finished).status_code == 409
    status = api.get(f"/api/jobs/{job_id}", headers=auth(USER)).json()
    assert status["status"] == "completed"
    assert status["output"]["content"] == "Hello back"
    workers = api.get("/api/admin/workers", headers=auth(ADMIN)).json()
    assert workers[0]["provisional_credits"] == 5

def test_privacy_requires_consent_and_trust(api):
    community_worker = enroll(api, "untrusted", trust="community")
    trusted_job = submit(api).json()["id"]
    assert api.post("/api/worker/claim", headers=auth(community_worker)).json()["job"] is None
    assert submit(api, "community", False).status_code == 422
    community_job = submit(api, "community", True).json()["id"]
    claimed = api.post("/api/worker/claim", headers=auth(community_worker)).json()["job"]
    assert claimed["id"] == community_job
    assert claimed["id"] != trusted_job

def test_authentication_refunds_on_terminal_failure(api):
    token = enroll(api, "desktop")
    assert submit(api).status_code == 201
    assert api.get("/api/balance").status_code == 401
    assert api.post("/api/admin/workers", headers=auth(USER), json={"name": "hack", "model": "demo-model"}).status_code == 403
    for i in range(3):
        claimed = api.post("/api/worker/claim", headers=auth(token)).json()["job"]
        assert claimed
        done = api.post("/api/worker/complete", headers=auth(token), json={"job_id": claimed["id"],
                     "lease_token": claimed["lease_token"], "ok": False, "error": "test error"})
        assert done.status_code == 200, done.text
    assert api.get("/api/balance", headers=auth(USER)).json()["demo_credits"] == 100
    assert api.get(f"/api/jobs/{claimed['id']}", headers=auth(USER)).json()["status"] == "failed"

def test_wrong_lease_cannot_complete(api):
    token = enroll(api, "desktop")
    submit(api)
    claimed = api.post("/api/worker/claim", headers=auth(token)).json()["job"]
    invalid = api.post("/api/worker/complete", headers=auth(token), json={"job_id": claimed["id"],
                        "lease_token": "wrong", "ok": True, "content": "bad"})
    assert invalid.status_code == 409
    assert api.get(f"/api/jobs/{claimed['id']}", headers=auth(USER)).json()["status"] == "running"

def test_worker_earn_then_admin_settle_then_spend(api):
    enrolled = api.post("/api/admin/workers", headers=auth(ADMIN), json={
        "name": "laptop", "model": "demo-model", "trust_level": "trusted"}).json()
    worker_key, user_key = enrolled["worker_token"], enrolled["user_token"]
    assert api.get("/api/balance", headers=auth(user_key)).json()["demo_credits"] == 0
    assert submit(api).status_code == 201
    claimed = api.post("/api/worker/claim", headers=auth(worker_key)).json()["job"]
    assert api.post("/api/worker/complete", headers=auth(worker_key), json={
        "job_id": claimed["id"], "lease_token": claimed["lease_token"],
        "ok": True, "content": "reply"}).status_code == 200
    assert api.get("/api/balance", headers=auth(user_key)).json()["demo_credits"] == 0
    assert api.post(f"/api/admin/workers/{enrolled['id']}/settle", headers=auth(USER), json={
        "credits": 5, "audit_note": "Admin reviewed"}).status_code == 403
    r = api.post(f"/api/admin/workers/{enrolled['id']}/settle", headers=auth(ADMIN), json={
        "credits": 5, "audit_note": "Manually checked test response"})
    assert r.status_code == 200, r.text
    assert api.get("/api/balance", headers=auth(user_key)).json()["demo_credits"] == 5
    assert api.post("/api/jobs", headers=auth(user_key), json={"model": "demo-model",
       "messages": [{"role": "user", "content": "My turn"}]}).status_code == 201
    assert api.get("/api/balance", headers=auth(user_key)).json()["demo_credits"] == 0

def test_lease_expiration_refund_only_once(api):
    token = enroll(api, "expiring")
    queued = submit(api).json()["id"]
    from datetime import datetime, timezone, timedelta
    for _ in range(3):
        job = api.post("/api/worker/claim", headers=auth(token)).json()["job"]
        assert job and job["id"] == queued
        # Simulate a worker crash and lease expiry; production waits for real time.
        with api.app.state.database.connect() as db:
            db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (
                (datetime.now(timezone.utc)-timedelta(seconds=5)).isoformat(timespec="microseconds"), queued))
    assert api.post("/api/worker/claim", headers=auth(token)).json()["job"] is None
    assert api.post("/api/worker/claim", headers=auth(token)).json()["job"] is None
    assert api.get("/api/balance", headers=auth(USER)).json()["demo_credits"] == 100
    assert api.get(f"/api/jobs/{queued}", headers=auth(USER)).json()["status"] == "failed"


def test_worker_token_is_not_user_token(api):
    token = enroll(api, "no-escalation")
    assert api.get("/api/balance", headers=auth(token)).status_code == 403
    assert api.post("/api/admin/workers", headers=auth(token), json={"name": "test", "model": "demo-model"}).status_code == 403


def test_non_ascii_bearer_is_rejected_not_server_error(api):
    header = {"Authorization": "Bearer ä".encode("latin-1")}
    assert api.get("/api/admin/workers", headers=header).status_code == 403
    assert api.get("/api/balance", headers=header).status_code == 403

def test_oversized_body_rejected_before_parsing_or_auth(api):
    big = b'{"model":"demo-model","messages":[{"role":"user","content":"' + b"x" * 2_000_000 + b'"}]}'
    assert api.post("/api/jobs", content=big, headers={"Content-Type": "application/json"}).status_code == 413

def test_oversized_chunked_body_rejected(api):
    def chunks():
        for _ in range(40):
            yield b"x" * 65536
    r = api.post("/api/jobs", content=chunks(), headers={**auth(USER), "Content-Type": "application/json"})
    assert r.status_code == 413
    assert api.get("/api/balance", headers=auth(USER)).json()["demo_credits"] == 100

def test_expired_lease_requeues_without_polling_worker(api):
    token = enroll(api, "flaky")
    queued = submit(api).json()["id"]
    claimed = api.post("/api/worker/claim", headers=auth(token)).json()["job"]
    expire_lease(api, queued)
    assert api.get(f"/api/jobs/{queued}", headers=auth(USER)).json()["status"] == "queued"
    late = {"job_id": queued, "lease_token": claimed["lease_token"], "ok": True, "content": "too late"}
    assert api.post("/api/worker/complete", headers=auth(token), json=late).status_code == 409

def test_exhausted_expired_job_refunded_without_polling_worker(api):
    token = enroll(api, "vanishing")
    queued = submit(api).json()["id"]
    for _ in range(3):
        assert api.post("/api/worker/claim", headers=auth(token)).json()["job"]["id"] == queued
        expire_lease(api, queued)
    # No worker polls any more; the user must still reach a terminal state and get the refund.
    assert api.get(f"/api/jobs/{queued}", headers=auth(USER)).json()["status"] == "failed"
    assert api.get("/api/balance", headers=auth(USER)).json()["demo_credits"] == 100
    assert api.get(f"/api/jobs/{queued}", headers=auth(USER)).json()["status"] == "failed"
    assert api.get("/api/balance", headers=auth(USER)).json()["demo_credits"] == 100


def test_concurrent_submits_cannot_overspend(clients):
    from concurrent.futures import ThreadPoolExecutor
    def spend(c):
        return [submit(c).status_code for _ in range(6)]
    with ThreadPoolExecutor(len(clients)) as pool:
        codes = [code for batch in pool.map(spend, clients) for code in batch]
    assert codes.count(201) == 20 and codes.count(402) == 16
    assert clients[0].get("/api/balance", headers=auth(USER)).json()["demo_credits"] == 0

def test_concurrent_claims_assign_each_job_once(clients):
    from concurrent.futures import ThreadPoolExecutor
    tokens = [enroll(clients[0], f"w{i}") for i in range(len(clients))]
    jobs = {submit(clients[0]).json()["id"] for _ in range(12)}
    def drain(pair):
        c, token = pair
        got = []
        while (job := c.post("/api/worker/claim", headers=auth(token)).json()["job"]):
            got.append(job["id"])
            assert c.post("/api/worker/complete", headers=auth(token), json={"job_id": job["id"],
                "lease_token": job["lease_token"], "ok": True, "content": "done"}).status_code == 200
        return got
    with ThreadPoolExecutor(len(clients)) as pool:
        claimed = [j for batch in pool.map(drain, zip(clients, tokens)) for j in batch]
    assert sorted(claimed) == sorted(jobs)
    workers = clients[0].get("/api/admin/workers", headers=auth(ADMIN)).json()
    assert sum(w["provisional_credits"] for w in workers) == 5 * len(jobs)


@pytest.mark.parametrize("operation", ["complete", "heartbeat", "claim"])
def test_lease_expiring_while_waiting_for_write_lock(api, monkeypatch, operation):
    """A request arriving before expiry must use the time after acquiring the lock."""
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from datetime import datetime, timedelta, timezone
    from threading import Event
    import coordinator.app.main as coordinator

    clock = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(coordinator, "now", lambda: clock[0])
    token = enroll(api, "lock-wait")
    job_id = submit(api).json()["id"]
    job = api.post("/api/worker/claim", headers=auth(token)).json()["job"]
    database = api.app.state.database
    connect = database.connect
    with connect() as db:
        db.execute("UPDATE jobs SET lease_until=?, attempts=3 WHERE id=?",
                   ((clock[0] + timedelta(seconds=1)).isoformat(timespec="microseconds"), job_id))

    writing = Event()

    @contextmanager
    def observed_connect():
        with connect() as db:
            # SQLite invokes the trace callback before the statement waits for a writer.
            db.set_trace_callback(lambda sql: writing.set() if sql.startswith(
                ("BEGIN IMMEDIATE", "UPDATE workers SET last_seen")) else None)
            yield db

    monkeypatch.setattr(database, "connect", observed_connect)
    payload = {"job_id": job_id, "lease_token": job["lease_token"]}
    if operation == "complete":
        payload.update(ok=True, content="late mock result")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with connect() as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            pending = pool.submit(api.post, f"/api/worker/{operation}",
                                  headers=auth(token), json=payload)
            try:
                assert writing.wait(timeout=5), "Request never reached the write lock"
                clock[0] += timedelta(seconds=2)
            finally:
                blocker.rollback()
        response = pending.result(timeout=10)

    if operation == "claim":
        assert response.status_code == 200
        assert response.json()["job"] is None
        # Inspect before a status request, which would independently trigger expiry.
        assert api.get("/api/balance", headers=auth(USER)).json()["demo_credits"] == 100
    else:
        assert response.status_code == 409
    assert api.get(f"/api/jobs/{job_id}", headers=auth(USER)).json()["status"] == "failed"
    assert api.get("/api/balance", headers=auth(USER)).json()["demo_credits"] == 100
    assert api.get("/api/admin/workers", headers=auth(ADMIN)).json()[0]["provisional_credits"] == 0


def test_lease_is_expired_at_its_exact_deadline(api, monkeypatch):
    from datetime import datetime, timezone
    import coordinator.app.main as coordinator

    token = enroll(api, "deadline")
    job_id = submit(api).json()["id"]
    api.post("/api/worker/claim", headers=auth(token))
    deadline = datetime(2030, 1, 1, tzinfo=timezone.utc)
    with api.app.state.database.connect() as db:
        db.execute("UPDATE jobs SET lease_until=?, attempts=3 WHERE id=?",
                   (deadline.isoformat(timespec="microseconds"), job_id))
    monkeypatch.setattr(coordinator, "now", lambda: deadline)
    assert api.get(f"/api/jobs/{job_id}", headers=auth(USER)).json()["status"] == "failed"
    assert api.get("/api/balance", headers=auth(USER)).json()["demo_credits"] == 100


def test_heartbeat_renews_only_the_assigned_workers_valid_lease(api, monkeypatch):
    from datetime import datetime, timedelta, timezone
    import coordinator.app.main as coordinator

    clock = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(coordinator, "now", lambda: clock[0])
    owner = enroll(api, "owner")
    other = enroll(api, "other")
    job_id = submit(api).json()["id"]
    job = api.post("/api/worker/claim", headers=auth(owner)).json()["job"]
    pulse = {"job_id": job_id, "lease_token": job["lease_token"]}
    assert api.post("/api/worker/heartbeat", headers=auth(other), json=pulse).status_code == 409
    assert api.post("/api/worker/heartbeat", headers=auth(owner), json={
        **pulse, "lease_token": "invalid"}).status_code == 409
    clock[0] += timedelta(seconds=170)
    assert api.post("/api/worker/heartbeat", headers=auth(owner), json=pulse).status_code == 200
    clock[0] += timedelta(seconds=20)  # Original lease has expired, renewed lease has not.
    assert api.post("/api/worker/complete", headers=auth(owner), json={
        **pulse, "ok": True, "content": "renewed mock result"}).status_code == 200


def test_concurrent_settlement_cannot_release_credits_twice(clients):
    from concurrent.futures import ThreadPoolExecutor

    api = clients[0]
    enrolled = api.post("/api/admin/workers", headers=auth(ADMIN), json={
        "name": "settlement", "model": "demo-model"}).json()
    submit(api)
    job = api.post("/api/worker/claim", headers=auth(enrolled["worker_token"])).json()["job"]
    assert api.post("/api/worker/complete", headers=auth(enrolled["worker_token"]), json={
        "job_id": job["id"], "lease_token": job["lease_token"],
        "ok": True, "content": "mock settlement result"}).status_code == 200

    def settle(client):
        return client.post(f"/api/admin/workers/{enrolled['id']}/settle", headers=auth(ADMIN),
                           json={"credits": 5, "audit_note": "Synthetic test review"}).status_code

    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        codes = list(pool.map(settle, clients))
    assert codes.count(200) == 1
    assert codes.count(409) == 5
    assert api.get("/api/balance", headers=auth(enrolled["user_token"])).json()["demo_credits"] == 5
    assert api.get("/api/admin/workers", headers=auth(ADMIN)).json()[0]["provisional_credits"] == 0


def test_concurrent_expiry_refunds_only_once(clients):
    from concurrent.futures import ThreadPoolExecutor

    api = clients[0]
    token = enroll(api, "refund")
    job_id = submit(api).json()["id"]
    api.post("/api/worker/claim", headers=auth(token))
    with api.app.state.database.connect() as db:
        db.execute("UPDATE jobs SET attempts=3 WHERE id=?", (job_id,))
    expire_lease(api, job_id)
    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        states = list(pool.map(lambda c: c.get(f"/api/jobs/{job_id}", headers=auth(USER)).json()["status"], clients))
    assert states == ["failed"] * 6
    assert api.get("/api/balance", headers=auth(USER)).json()["demo_credits"] == 100
    with api.app.state.database.connect() as db:
        refunds = db.execute("SELECT delta FROM ledger WHERE job_id=? AND note='refund_expired_job'", (job_id,)).fetchall()
    assert [row["delta"] for row in refunds] == [5]


def _issue_enroll_code(api, ttl_seconds=3600):
    r = api.post("/api/admin/enroll-codes", headers=auth(ADMIN), json={"ttl_seconds": ttl_seconds})
    assert r.status_code == 200, r.text
    return r.json()["code"]


def test_self_enroll_code_is_single_use(api):
    code = _issue_enroll_code(api)
    first = api.post("/api/enroll", json={"code": code, "name": "dev-one", "model": "demo-model"})
    assert first.status_code == 201, first.text
    second = api.post("/api/enroll", json={"code": code, "name": "dev-two", "model": "demo-model"})
    assert second.status_code == 403, second.text


def test_self_enroll_rejects_unknown_code(api):
    r = api.post("/api/enroll", json={"code": "not-a-real-code", "name": "dev", "model": "demo-model"})
    assert r.status_code == 403


def test_self_enroll_rejects_expired_code(api):
    code = _issue_enroll_code(api)
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="microseconds")
    with api.app.state.database.connect() as db:
        db.execute("UPDATE enroll_codes SET expires_at=? WHERE code_hash=?", (past, digest(code)))
    r = api.post("/api/enroll", json={"code": code, "name": "dev", "model": "demo-model"})
    assert r.status_code == 403


def test_self_enroll_throttled_per_source(make_api):
    api = make_api(enroll_ip_per_minute=1)
    code_a, code_b = _issue_enroll_code(api), _issue_enroll_code(api)
    first = api.post("/api/enroll", json={"code": code_a, "name": "dev-a", "model": "demo-model"})
    assert first.status_code == 201, first.text
    second = api.post("/api/enroll", json={"code": code_b, "name": "dev-b", "model": "demo-model"})
    assert second.status_code == 429, second.text


def test_worker_me_reports_own_provisional_credits(api):
    token = enroll(api, "self-status")
    me = api.get("/api/worker/me", headers=auth(token)).json()
    assert me["name"] == "self-status" and me["provisional_credits"] == 0
    job_id = submit(api).json()["id"]
    claim = api.post("/api/worker/claim", headers=auth(token)).json()["job"]
    api.post("/api/worker/complete", headers=auth(token),
             json={"job_id": job_id, "lease_token": claim["lease_token"], "ok": True, "content": "hi"})
    assert api.get("/api/worker/me", headers=auth(token)).json()["provisional_credits"] == 5
    assert "user_id" not in api.get("/api/worker/me", headers=auth(token)).json()


def test_worker_me_requires_worker_token(api):
    assert api.get("/api/worker/me", headers=auth(USER)).status_code == 403
