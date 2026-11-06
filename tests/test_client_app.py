"""Local ProjectZero app against an in-process coordinator (no network, mock model)."""
import json
import os
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from coordinator.app.main import Settings, create_app as create_coordinator
from grid_helpers import ADMIN, USER
from projectzero.app import create_app as create_local
from projectzero.config import Config, check_coordinator_url, clamp_settings

COORD = "http://coord.local:8765"
SESSION = "test-session-secret"


@pytest.fixture
def stack(tmp_path):
    settings = Settings(admin_key=ADMIN, user_key=USER, db_path=str(tmp_path / "c.sqlite3"),
                        registration="open", auto_settle=True, signup_credits=20)
    coordinator_app = create_coordinator(settings)
    with TestClient(coordinator_app):  # runs the coordinator lifespan (database setup)
        transport = httpx.ASGITransport(app=coordinator_app)
        config = Config(tmp_path / "client")
        local = create_local(config, port=8790, session=SESSION, transport=transport)
        with TestClient(local, base_url="http://127.0.0.1:8790",
                        headers={"X-PZ-Session": SESSION}) as client:
            yield client, config, coordinator_app


def test_guard_rejects_foreign_host_and_missing_session(stack):
    client, _, _ = stack
    assert client.get("/local/state", headers={"host": "evil.example:8790"}).status_code == 421
    assert client.get("/local/state", headers={"X-PZ-Session": "wrong"}).status_code == 403
    page = client.get("/", headers={"X-PZ-Session": ""})
    assert page.status_code == 200 and SESSION in page.text and "__PZ_SESSION__" not in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]


def test_register_machine_mock_job_and_credits(stack):
    client, config, _ = stack
    assert client.post("/local/connect", json={"coordinator_url": COORD}).json()["registration"] == "open"
    reg = client.post("/local/register", json={"name": "alice"})
    assert reg.status_code == 200, reg.text
    assert reg.json()["credits"] == 20
    assert config.get("user_token").startswith("cag_usr_")
    state = client.get("/local/state").json()
    assert state["signed_in"] and state["name"] == "alice"
    assert "user_token" not in json.dumps(state) and "cag_usr_" not in json.dumps(state)

    machine = client.post("/local/machine", json={"name": "Testrechner", "model": "demo-model"})
    assert machine.status_code == 200, machine.text
    assert "worker_token" not in machine.json()
    assert "worker_token" not in json.dumps(client.get("/local/state").json())
    on = client.post("/local/settings", json={"contribute": True, "backend": "mock", "cpu_threads": 99})
    assert on.json()["settings"]["cpu_threads"] == 8  # clamped

    job = client.post("/api/jobs", json={"model": "demo-model", "privacy": "community", "allow_untrusted": True,
                                         "messages": [{"role": "user", "content": "Hallo Netz"}]})
    assert job.status_code == 201, job.text
    deadline, status = time.monotonic() + 20, None
    while time.monotonic() < deadline:
        status = client.get(f"/api/jobs/{job.json()['id']}").json()
        if status["status"] == "completed":
            break
        time.sleep(0.3)
    assert status["status"] == "completed", status
    assert "Hallo Netz" in status["output"]["content"]
    me = client.get("/api/me").json()
    assert me["credits"] == 20  # paid 5, earned 5 back through auto-settle
    assert me["earned_credits"] == 5
    assert client.get("/local/state").json()["worker"]["jobs_completed"] == 1
    off = client.post("/local/settings", json={"contribute": False})
    assert off.json()["worker"]["running"] is False


def test_proxy_allowlist_and_social(stack):
    client, _, _ = stack
    client.post("/local/connect", json={"coordinator_url": COORD})
    client.post("/local/register", json={"name": "bob"})
    assert client.get("/api/admin/workers").status_code == 404
    assert client.post("/api/me/rotate-token").status_code == 404
    assert client.get("/api/../health").status_code == 404
    lobby = client.post("/api/channels/lobby/messages", json={"body": "Hallo zusammen"})
    assert lobby.status_code == 201, lobby.text
    assert client.get("/api/channels/lobby/messages").json()[-1]["body"] == "Hallo zusammen"


def test_connect_validates_url(stack):
    client, _, _ = stack
    assert client.post("/local/connect", json={"coordinator_url": "http://example.com:8765"}).status_code == 422
    assert client.post("/local/connect", json={"coordinator_url": "ftp://coord.local"}).status_code == 422


@pytest.mark.parametrize("url,ok", [
    ("https://grid.example.org", True), ("http://127.0.0.1:8765", True), ("http://192.168.1.5:8765", True),
    ("http://100.100.1.2:8765", True), ("http://nas.local:8765/", True), ("http://8.8.8.8:8765", False),
    ("http://user:pw@10.0.0.1", False), ("javascript:alert(1)", False)])
def test_check_coordinator_url(url, ok):
    if ok:
        assert check_coordinator_url(url).startswith(("http://", "https://"))
    else:
        with pytest.raises(ValueError):
            check_coordinator_url(url)


def test_clamp_settings_and_private_config_file(tmp_path):
    assert clamp_settings({"gpu_layers": -5, "backend": "rm -rf", "context_tokens": "x"})["gpu_layers"] == 0
    assert clamp_settings({"backend": "rm -rf"})["backend"] == "ollama"
    config = Config(tmp_path / "c")
    config.update(user_token="cag_usr_secret")
    probe = tmp_path / "probe"
    probe.write_text("x")
    probe.chmod(0o600)
    if os.name == "posix" and probe.stat().st_mode & 0o077 == 0:  # some mounts (fuseblk) ignore chmod
        assert (config.path.stat().st_mode & 0o077) == 0
    assert Config(tmp_path / "c").get("user_token") == "cag_usr_secret"
