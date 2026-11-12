"""Per-job lifecycle log: lets a requester see what is actually happening with their own request
(queued/claimed/completed/requeued/failed/cancelled, and which worker handled it) without
exposing worker identity to anyone but the job's own owner."""
from datetime import timedelta

from coordinator.app.main import MAX_ATTEMPTS
from grid_helpers import ADMIN, USER, auth, enroll, enroll_full, freeze_clock, submit


def event_types(status: dict) -> list[str]:
    return [e["type"] for e in status["events"]]


def test_fresh_job_starts_with_a_queued_event_and_no_available_workers(api):
    job_id = submit(api).json()["id"]
    status = api.get(f"/api/jobs/{job_id}", headers=auth(USER)).json()
    assert event_types(status) == ["queued"]
    assert status["available_workers"] == 0


def test_available_workers_counts_only_online_matching_workers(api):
    enroll(api, "never-polled")  # enrollment alone never sets last_seen
    job_id = submit(api).json()["id"]
    status = api.get(f"/api/jobs/{job_id}", headers=auth(USER)).json()
    assert status["available_workers"] == 0


def test_claim_and_complete_record_worker_name_not_id(api):
    w = enroll_full(api, "cruncher")
    api.post("/api/worker/heartbeat", headers=auth(w["worker_token"]), json={})
    job_id = submit(api).json()["id"]
    status = api.get(f"/api/jobs/{job_id}", headers=auth(USER)).json()
    assert status["available_workers"] == 1
    job = api.post("/api/worker/claim", headers=auth(w["worker_token"])).json()["job"]
    status = api.get(f"/api/jobs/{job_id}", headers=auth(USER)).json()
    assert event_types(status) == ["queued", "claimed"]
    assert status["events"][1]["worker"] == "cruncher"
    assert "worker_id" not in status["events"][1] and w["id"] not in str(status)
    api.post("/api/worker/complete", headers=auth(w["worker_token"]),
             json={"job_id": job_id, "lease_token": job["lease_token"], "ok": True, "content": "done"})
    status = api.get(f"/api/jobs/{job_id}", headers=auth(USER)).json()
    assert event_types(status) == ["queued", "claimed", "completed"]
    assert status["events"][2]["worker"] == "cruncher"


def test_failed_attempt_is_requeued_then_finally_failed_with_worker_and_error(make_api, monkeypatch):
    clock = freeze_clock(monkeypatch)
    api = make_api(lease_seconds=60)
    w = enroll_full(api, "flaky")
    job_id = submit(api).json()["id"]
    for _ in range(MAX_ATTEMPTS - 1):  # earlier failures requeue, the last one fails for good
        job = api.post("/api/worker/claim", headers=auth(w["worker_token"])).json()["job"]
        api.post("/api/worker/complete", headers=auth(w["worker_token"]),
                 json={"job_id": job_id, "lease_token": job["lease_token"], "ok": False, "error": "boom"})
        clock[0] += timedelta(seconds=1)
    job = api.post("/api/worker/claim", headers=auth(w["worker_token"])).json()["job"]
    api.post("/api/worker/complete", headers=auth(w["worker_token"]),
             json={"job_id": job_id, "lease_token": job["lease_token"], "ok": False, "error": "boom"})
    status = api.get(f"/api/jobs/{job_id}", headers=auth(USER)).json()
    assert event_types(status) == ["queued", "claimed", "requeued", "claimed", "requeued", "claimed", "failed"]
    assert all(e["worker"] == "flaky" for e in status["events"] if e["type"] in ("claimed", "requeued", "failed"))
    assert all(e.get("error") == "boom" for e in status["events"] if e["type"] in ("requeued", "failed"))


def test_cancelled_job_records_a_cancelled_event(api):
    job_id = submit(api).json()["id"]
    api.post(f"/api/jobs/{job_id}/cancel", headers=auth(USER))
    status = api.get(f"/api/jobs/{job_id}", headers=auth(USER)).json()
    assert event_types(status) == ["queued", "cancelled"]


def test_available_workers_respects_trust_and_privacy(api):
    trusted = enroll_full(api, "t", trust="trusted")
    community_w = enroll_full(api, "c", trust="community")
    api.post("/api/worker/heartbeat", headers=auth(trusted["worker_token"]), json={})
    api.post("/api/worker/heartbeat", headers=auth(community_w["worker_token"]), json={})
    trusted_job = submit(api, privacy="trusted").json()["id"]
    community_job = submit(api, privacy="community", allow_untrusted=True).json()["id"]
    assert api.get(f"/api/jobs/{trusted_job}", headers=auth(USER)).json()["available_workers"] == 1
    assert api.get(f"/api/jobs/{community_job}", headers=auth(USER)).json()["available_workers"] == 2


def test_queue_timeout_records_a_failed_event_without_a_worker(make_api, monkeypatch):
    clock = freeze_clock(monkeypatch)
    api = make_api(queue_timeout_seconds=60)
    job_id = submit(api).json()["id"]
    clock[0] += timedelta(seconds=61)
    status = api.get(f"/api/jobs/{job_id}", headers=auth(USER)).json()
    assert event_types(status) == ["queued", "failed"]
    assert "worker" not in status["events"][1]
    assert status["events"][1]["error"] == "Queue timeout: no matching worker"


def test_revoke_requeues_with_an_event_naming_the_revoked_worker(api):
    w = enroll_full(api, "going-away")
    job_id = submit(api).json()["id"]
    api.post("/api/worker/claim", headers=auth(w["worker_token"]))
    api.post(f"/api/admin/workers/{w['id']}/revoke", headers=auth(ADMIN))
    status = api.get(f"/api/jobs/{job_id}", headers=auth(USER)).json()
    assert event_types(status) == ["queued", "claimed", "requeued"]
    assert status["events"][2]["worker"] == "going-away" and status["events"][2]["error"] == "Worker revoked"
