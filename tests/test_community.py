import pytest

from coordinator.app import community
from grid_helpers import ADMIN, USER, auth, submit


def register(api, name, invite_code=None):
    payload = {"name": name}
    if invite_code is not None:
        payload["invite_code"] = invite_code
    return api.post("/api/register", json=payload)


def register_ok(api, name, invite_code=None):
    r = register(api, name, invite_code)
    assert r.status_code == 201, r.text
    return r.json()


def add_machine(api, user_token, name, model="demo-model"):
    return api.post("/api/me/machines", headers=auth(user_token), json={"name": name, "model": model})


def add_machine_ok(api, user_token, name, model="demo-model"):
    r = add_machine(api, user_token, name, model)
    assert r.status_code == 201, r.text
    return r.json()


def new_invite(api, uses=1, ttl_seconds=86400):
    r = api.post("/api/admin/invites", headers=auth(ADMIN), json={"uses": uses, "ttl_seconds": ttl_seconds})
    assert r.status_code == 200, r.text
    return r.json()["code"]


def ledger_rows(api, job_id=None, note=None):
    with api.app.state.database.connect() as db:
        if job_id is not None:
            return db.execute("SELECT * FROM ledger WHERE job_id=?", (job_id,)).fetchall()
        return db.execute("SELECT * FROM ledger WHERE note=?", (note,)).fetchall()


# ---------------------------------------------------------------- /api/register


def test_register_disabled_by_default(api):
    assert register(api, "someone").status_code == 404


def test_register_rejects_bad_name_pattern(api):
    assert register(api, "ab").status_code == 422  # below the 3-char minimum


def test_register_open_grants_signup_credits_with_ledger_row(make_api):
    api = make_api(registration="open")
    out = register_ok(api, "alice")
    assert out["credits"] == 20
    rows = ledger_rows(api, note="signup_credits")
    assert len(rows) == 1 and rows[0]["delta"] == 20 and rows[0]["actor_id"] == out["id"]


def test_register_invite_mode_requires_a_valid_code(make_api):
    api = make_api(registration="invite")
    assert register(api, "bob").status_code == 403
    code = new_invite(api, uses=1)
    assert register(api, "bob", invite_code=code).status_code == 201
    assert register(api, "carol", invite_code=code).status_code == 403  # already used up


def test_register_duplicate_name_case_insensitive(make_api):
    api = make_api(registration="open")
    assert register(api, "Foo").status_code == 201
    assert register(api, "foo").status_code == 409


def test_register_reserved_name(make_api):
    api = make_api(registration="open")
    assert register(api, "admin").status_code == 409
    assert register(api, "lobby").status_code == 409


def test_register_per_source_rate_limit(make_api):
    api = make_api(registration="open", register_per_hour=2)
    assert register(api, "u-one").status_code == 201
    assert register(api, "u-two").status_code == 201
    assert register(api, "u-three").status_code == 429


# ---------------------------------------------------------------- /api/me


def test_me_shows_credits_and_machines(make_api):
    api = make_api(registration="open")
    alice = register_ok(api, "alice")
    out = api.get("/api/me", headers=auth(alice["user_token"])).json()
    assert out["name"] == "alice" and out["credits"] == 20 and out["machines"] == []
    machine = add_machine_ok(api, alice["user_token"], "alice-box")
    out = api.get("/api/me", headers=auth(alice["user_token"])).json()
    assert [m["id"] for m in out["machines"]] == [machine["id"]]


def test_me_rename_and_conflict(make_api):
    api = make_api(registration="open")
    alice, bob = register_ok(api, "alice"), register_ok(api, "bob")
    r = api.patch("/api/me", headers=auth(alice["user_token"]), json={"name": "carol"})
    assert r.status_code == 200 and r.json() == {"name": "carol"}
    assert api.get("/api/me", headers=auth(alice["user_token"])).json()["name"] == "carol"
    assert api.patch("/api/me", headers=auth(bob["user_token"]), json={"name": "carol"}).status_code == 409


def test_me_rotate_token(make_api):
    api = make_api(registration="open")
    alice = register_ok(api, "alice")
    old = alice["user_token"]
    r = api.post("/api/me/rotate-token", headers=auth(old))
    assert r.status_code == 200
    new = r.json()["user_token"]
    assert new != old
    assert api.get("/api/me", headers=auth(old)).status_code == 403
    assert api.get("/api/me", headers=auth(new)).status_code == 200


# ---------------------------------------------------------------- /api/me/machines


def test_add_machine_requires_a_display_name(api):
    assert add_machine(api, USER, "nameless-box").status_code == 403


def test_machine_is_community_trust_and_claims_only_community_jobs(make_api):
    api = make_api(registration="open")
    alice = register_ok(api, "alice")
    machine = add_machine_ok(api, alice["user_token"], "alice-box")
    assert machine["trust_level"] == "community"
    trusted_job = submit(api, privacy="trusted").json()["id"]
    assert api.post("/api/worker/claim", headers=auth(machine["worker_token"])).json()["job"] is None
    community_job = submit(api, privacy="community", allow_untrusted=True).json()["id"]
    claimed = api.post("/api/worker/claim", headers=auth(machine["worker_token"])).json()["job"]
    assert claimed["id"] == community_job != trusted_job


def test_max_machines_per_member_enforced(make_api):
    api = make_api(registration="open", max_machines_per_member=1)
    alice = register_ok(api, "alice")
    add_machine_ok(api, alice["user_token"], "m1")
    assert add_machine(api, alice["user_token"], "m2").status_code == 409


def test_delete_machine_revokes_it_and_requeues_its_running_job(make_api):
    api = make_api(registration="open")
    alice = register_ok(api, "alice")
    machine = add_machine_ok(api, alice["user_token"], "alice-box")
    job_id = submit(api, privacy="community", allow_untrusted=True).json()["id"]
    claimed = api.post("/api/worker/claim", headers=auth(machine["worker_token"])).json()["job"]
    assert claimed["id"] == job_id
    r = api.delete(f"/api/me/machines/{machine['id']}", headers=auth(alice["user_token"]))
    assert r.status_code == 200 and r.json()["requeued_jobs"] == 1
    assert api.get(f"/api/jobs/{job_id}", headers=auth(USER)).json()["status"] == "queued"
    assert api.post("/api/worker/claim", headers=auth(machine["worker_token"])).status_code == 403


# ---------------------------------------------------------------- settlement


def test_auto_settle_credits_owner_spendable_balance_not_provisional(make_api):
    api = make_api(registration="open", auto_settle=True)
    alice = register_ok(api, "alice")
    machine = add_machine_ok(api, alice["user_token"], "alice-box")
    job_id = submit(api, privacy="community", allow_untrusted=True).json()["id"]
    claimed = api.post("/api/worker/claim", headers=auth(machine["worker_token"])).json()["job"]
    r = api.post("/api/worker/complete", headers=auth(machine["worker_token"]), json={
        "job_id": job_id, "lease_token": claimed["lease_token"], "ok": True, "content": "hi"})
    assert r.status_code == 200
    me = api.get("/api/me", headers=auth(alice["user_token"])).json()
    assert me["credits"] == 25 and me["machines"][0]["provisional_credits"] == 0
    rows = ledger_rows(api, job_id=job_id)
    settle_row = [row for row in rows if row["note"] == "auto_settled_unverified"]
    assert len(settle_row) == 1 and settle_row[0]["actor_id"] == alice["id"]


def test_without_auto_settle_admin_settles_the_owner_not_the_worker_id(make_api):
    api = make_api(registration="open")  # auto_settle defaults to False
    alice = register_ok(api, "alice")
    machine = add_machine_ok(api, alice["user_token"], "alice-box")
    job_id = submit(api, privacy="community", allow_untrusted=True).json()["id"]
    claimed = api.post("/api/worker/claim", headers=auth(machine["worker_token"])).json()["job"]
    api.post("/api/worker/complete", headers=auth(machine["worker_token"]), json={
        "job_id": job_id, "lease_token": claimed["lease_token"], "ok": True, "content": "hi"})
    before = api.get("/api/me", headers=auth(alice["user_token"])).json()
    assert before["credits"] == 20 and before["machines"][0]["provisional_credits"] == 5
    r = api.post(f"/api/admin/workers/{machine['id']}/settle", headers=auth(ADMIN),
                 json={"credits": 5, "audit_note": "verified manually ok"})
    assert r.status_code == 200
    assert r.json()["user_id"] == alice["id"] != machine["id"]
    after = api.get("/api/me", headers=auth(alice["user_token"])).json()
    assert after["credits"] == 25 and after["machines"][0]["provisional_credits"] == 0


# ---------------------------------------------------------------- /api/online


def test_online_lists_machine_after_heartbeat_and_active_people(make_api):
    api = make_api(registration="open")
    alice = register_ok(api, "alice")
    machine = add_machine_ok(api, alice["user_token"], "alice-box")
    before = api.get("/api/online", headers=auth(alice["user_token"])).json()
    assert before["machines"] == []  # never heartbeated: not online yet
    api.post("/api/worker/heartbeat", headers=auth(machine["worker_token"]), json={})
    after = api.get("/api/online", headers=auth(alice["user_token"])).json()
    assert after["machines"][0]["name"] == "alice-box"
    assert after["machines"][0]["owner"] == "alice"
    assert "alice" in after["people"]


# ---------------------------------------------------------------- channels


def test_lobby_is_visible_to_every_named_member(make_api):
    api = make_api(registration="open")
    alice, bob = register_ok(api, "alice"), register_ok(api, "bob")
    for token in (alice["user_token"], bob["user_token"]):
        ids = {c["id"] for c in api.get("/api/channels", headers=auth(token)).json()}
        assert "lobby" in ids


def test_group_membership_gates_read_and_post(make_api):
    api = make_api(registration="open")
    alice, bob, carol = (register_ok(api, n) for n in ("alice", "bob", "carol"))
    group = api.post("/api/channels", headers=auth(alice["user_token"]),
                     json={"name": "Team", "members": ["bob"]}).json()
    gid = group["id"]
    assert api.get(f"/api/channels/{gid}/messages", headers=auth(carol["user_token"])).status_code == 404
    assert api.post(f"/api/channels/{gid}/messages", headers=auth(carol["user_token"]),
                    json={"body": "hi"}).status_code == 404
    r = api.post(f"/api/channels/{gid}/members", headers=auth(alice["user_token"]), json={"name": "carol"})
    assert r.status_code == 200 and r.json() == {"added": "carol"}
    assert api.get(f"/api/channels/{gid}/messages", headers=auth(carol["user_token"])).status_code == 200
    assert api.post(f"/api/channels/{gid}/leave", headers=auth(bob["user_token"])).status_code == 200
    assert api.get(f"/api/channels/{gid}/messages", headers=auth(bob["user_token"])).status_code == 404


def test_direct_channel_is_idempotent_both_directions(make_api):
    api = make_api(registration="open")
    alice, bob = register_ok(api, "alice"), register_ok(api, "bob")
    a_to_b = api.post("/api/direct", headers=auth(alice["user_token"]), json={"name": "bob"}).json()["id"]
    b_to_a = api.post("/api/direct", headers=auth(bob["user_token"]), json={"name": "alice"}).json()["id"]
    assert a_to_b == b_to_a


def test_message_paging_after_and_newest_page(make_api):
    api = make_api(registration="open")
    alice = register_ok(api, "alice")
    ids = []
    for i in range(5):
        r = api.post("/api/channels/lobby/messages", headers=auth(alice["user_token"]), json={"body": f"m{i}"})
        assert r.status_code == 201
        ids.append(r.json()["id"])
    newest = api.get("/api/channels/lobby/messages", headers=auth(alice["user_token"]), params={"limit": 2}).json()
    assert [m["body"] for m in newest] == ["m3", "m4"]
    rest = api.get("/api/channels/lobby/messages", headers=auth(alice["user_token"]),
                   params={"after": ids[1], "limit": 10}).json()
    assert [m["body"] for m in rest] == ["m2", "m3", "m4"]


def test_message_body_too_long_rejected(make_api):
    api = make_api(registration="open")
    alice = register_ok(api, "alice")
    r = api.post("/api/channels/lobby/messages", headers=auth(alice["user_token"]), json={"body": "x" * 2001})
    assert r.status_code == 422


def test_message_pruning_keeps_only_the_newest(make_api, monkeypatch):
    monkeypatch.setattr(community, "KEEP_MESSAGES", 3)
    api = make_api(registration="open")
    alice = register_ok(api, "alice")
    for i in range(5):
        api.post("/api/channels/lobby/messages", headers=auth(alice["user_token"]), json={"body": f"m{i}"})
    kept = api.get("/api/channels/lobby/messages", headers=auth(alice["user_token"]), params={"limit": 10}).json()
    assert [m["body"] for m in kept] == ["m2", "m3", "m4"]


# ---------------------------------------------------------------- admin moderation


def test_admin_delete_message(make_api):
    api = make_api(registration="open")
    alice = register_ok(api, "alice")
    msg_id = api.post("/api/channels/lobby/messages", headers=auth(alice["user_token"]),
                      json={"body": "oops"}).json()["id"]
    r = api.delete(f"/api/admin/messages/{msg_id}", headers=auth(ADMIN))
    assert r.status_code == 200 and r.json() == {"deleted": msg_id}
    assert api.delete(f"/api/admin/messages/{msg_id}", headers=auth(ADMIN)).status_code == 404
    remaining = api.get("/api/channels/lobby/messages", headers=auth(alice["user_token"])).json()
    assert msg_id not in [m["id"] for m in remaining]


def test_admin_block_user_invalidates_token_and_revokes_machines(make_api):
    api = make_api(registration="open")
    alice = register_ok(api, "alice")
    machine = add_machine_ok(api, alice["user_token"], "alice-box")
    r = api.post("/api/admin/users/alice/block", headers=auth(ADMIN))
    assert r.status_code == 200 and r.json() == {"blocked": "alice", "revoked_machines": 1}
    assert api.get("/api/me", headers=auth(alice["user_token"])).status_code == 403
    assert api.post("/api/worker/claim", headers=auth(machine["worker_token"])).status_code == 403
    workers = api.get("/api/admin/workers", headers=auth(ADMIN)).json()
    assert next(w for w in workers if w["id"] == machine["id"])["revoked_at"] is not None


# ---------------------------------------------------------------- regressions from the 2026-11-06 security review (fixed)
#
# community.py is off-limits for this branch tonight (owned by the release/projectzero session) —
# these document confirmed gaps as xfail instead of fixing them. Both reproduced directly against
# the running app before being written down here; remove the xfail marker once main.py/community.py
# fix the underlying issue, so a regression here starts failing the suite again.


def test_demo_user_cannot_use_member_self_service_when_registration_is_off(api):
    assert api.patch("/api/me", headers=auth(USER), json={"name": "demo-renamed"}).status_code == 403
    assert add_machine(api, USER, "shadow-box").status_code == 403
    assert api.post("/api/me/rotate-token", headers=auth(USER)).status_code == 403


def test_block_user_revokes_their_own_unowned_worker_too(api):
    w = api.post("/api/admin/workers", headers=auth(ADMIN),
                 json={"name": "trusty", "model": "demo-model", "trust_level": "trusted"}).json()
    # Named directly in the DB (not via PATCH /api/me) to isolate this from the separate gap
    # above — any admin-enrolled worker's own user row can end up named this way regardless.
    with api.app.state.database.connect() as db:
        db.execute("UPDATE users SET name=? WHERE id=?", ("trusty-member", w["id"]))
    r = api.post("/api/admin/users/trusty-member/block", headers=auth(ADMIN))
    assert r.status_code == 200 and r.json()["revoked_machines"] == 1
    assert api.post("/api/worker/claim", headers=auth(w["worker_token"])).status_code == 403
