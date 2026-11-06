import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import grid_device  # noqa: E402
import grid_join  # noqa: E402


def public_key(key_type="ssh-ed25519", payload=b"\x01" * 32):
    blob = len(key_type).to_bytes(4, "big") + key_type.encode() + len(payload).to_bytes(4, "big") + payload
    return f"{key_type} {base64.b64encode(blob).decode()} someone@example"


def test_parse_public_key_accepts_ed25519_and_drops_comment():
    key_type, key_b64 = grid_device.parse_public_key(public_key() + "\n")
    assert key_type == "ssh-ed25519" and key_b64 in public_key()


@pytest.mark.parametrize("text", [
    "", public_key() + "\n" + public_key(), "ssh-rsa AAAAB3NzaC1yc2E= x",
    'command="/bin/sh" ' + public_key(), "ssh-ed25519 not-base64!!", "ssh-ed25519 AAAA",
    public_key().replace("ssh-ed25519", "ecdsa-sha2-nistp256", 1)])
def test_parse_public_key_rejects_options_multiple_and_mismatched_keys(text):
    with pytest.raises(ValueError):
        grid_device.parse_public_key(text)


def test_key_line_only_allows_one_forward():
    line = grid_device.key_line("laptop", "w1", "ssh-ed25519", "AAAA", 8765)
    options = line.split(" ")[0]
    assert options.startswith("restrict,")
    assert 'permitopen="127.0.0.1:8765"' in options and 'command="/usr/sbin/nologin"' in options
    assert line.endswith(" cag-device:laptop:w1")


def test_without_device_removes_only_that_device():
    content = "\n".join([grid_device.key_line(name, f"w-{name}", "ssh-ed25519", "AAAA", 8765)
                         for name in ("laptop", "laptop2", "pi")]) + "\n"
    remaining = grid_device.without_device(content, "laptop")
    assert "cag-device:laptop:" not in remaining
    assert "cag-device:laptop2:" in remaining and "cag-device:pi:" in remaining


def honors_chmod(path):
    probe = path / "chmod-probe"
    probe.write_text("")
    probe.chmod(0o600)
    return os.name == "posix" and probe.stat().st_mode & 0o777 == 0o600


def make_args(tmp_path, **overrides):
    if not (tmp_path / "authorized_keys").exists():
        (tmp_path / "device.pub").write_text(public_key())
        (tmp_path / "host.pub").write_text(public_key(payload=b"\x02" * 32).replace("someone@example", "root@host"))
        (tmp_path / "authorized_keys").write_text("")
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    values = dict(name="laptop", pubkey=tmp_path / "device.pub", host="desk.example", ssh_port=22,
                  tunnel_user="gridtunnel", host_key=tmp_path / "host.pub", model="qwen2.5-coder:1.5b",
                  trust="trusted", authorized_keys=tmp_path / "authorized_keys", state=state, coordinator_port=8765)
    values.update(overrides)
    return argparse.Namespace(**values)


def coordinator(seen, enroll_status=200):
    def handler(request):
        seen.append((request.method, request.url.path, request.headers["Authorization"],
                     json.loads(request.content) if request.content else None))
        if request.url.path == "/api/admin/workers":
            return httpx.Response(enroll_status, json={"id": "w1", "worker_token": "cag_synthetic", "user_token": "u"})
        return httpx.Response(200, json={"revoked": True})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_add_enrolls_writes_restricted_key_and_private_bundle(tmp_path):
    args, seen = make_args(tmp_path), []
    with coordinator(seen) as client:
        result = grid_device.add(args, client, "http://127.0.0.1:8765", "synthetic-admin")
    assert seen == [("POST", "/api/admin/workers", "Bearer synthetic-admin",
                     {"name": "laptop", "model": "qwen2.5-coder:1.5b", "trust_level": "trusted"})]
    keys = (tmp_path / "authorized_keys").read_text().splitlines()
    assert len(keys) == 1 and keys[0].startswith("restrict,") and keys[0].endswith("cag-device:laptop:w1")
    assert "someone@example" not in keys[0]
    path = Path(result["bundle"])
    if honors_chmod(tmp_path):
        assert path.stat().st_mode & 0o777 == 0o600
    bundle = path.read_text()
    assert "CAG_WORKER_TOKEN=cag_synthetic" in bundle and "CAG_TUNNEL_HOST=desk.example" in bundle
    assert "CAG_TUNNEL_HOST_KEY=ssh-ed25519 " in bundle and "root@host" not in bundle
    assert json.loads((args.state / "registry.json").read_text()) == {
        "laptop": {"worker_id": "w1", "model": "qwen2.5-coder:1.5b", "trust_level": "trusted"}}


def test_add_refuses_duplicates_before_enrolling(tmp_path):
    args, seen = make_args(tmp_path), []
    with coordinator(seen) as client:
        grid_device.add(args, client, "http://127.0.0.1:8765", "a")
        with pytest.raises(ValueError, match="already exists"):
            grid_device.add(args, client, "http://127.0.0.1:8765", "a")
        with pytest.raises(ValueError, match="already registered"):
            grid_device.add(make_args(tmp_path, name="other"), client, "http://127.0.0.1:8765", "a")
    assert len(seen) == 1


@pytest.mark.parametrize("override", [{"name": "Bad Name"}, {"host": "-oProxyCommand=x"}, {"name": "-x"}])
def test_add_rejects_unsafe_names_and_hosts(tmp_path, override):
    with coordinator([]) as client, pytest.raises(ValueError):
        grid_device.add(make_args(tmp_path, **override), client, "http://127.0.0.1:8765", "a")


def test_add_revokes_token_when_key_cannot_be_written(tmp_path, monkeypatch):
    args, seen = make_args(tmp_path), []
    def fail(path, content):
        raise OSError("disk full")
    monkeypatch.setattr(grid_device, "write_keys", fail)
    with coordinator(seen) as client, pytest.raises(OSError):
        grid_device.add(args, client, "http://127.0.0.1:8765", "a")
    assert [path for _, path, _, _ in seen] == ["/api/admin/workers", "/api/admin/workers/w1/revoke"]
    assert not (args.state / "registry.json").exists()


def test_remove_deletes_key_bundle_and_revokes(tmp_path):
    args, seen = make_args(tmp_path), []
    with coordinator(seen) as client:
        grid_device.add(args, client, "http://127.0.0.1:8765", "a")
        result = grid_device.remove(args, client, "http://127.0.0.1:8765", "a")
    assert result == {"device": "laptop", "worker_id": "w1", "key_removed": True, "revoked": True}
    assert (tmp_path / "authorized_keys").read_text() == ""
    assert not (args.state / "laptop.env").exists()
    assert seen[-1][1] == "/api/admin/workers/w1/revoke"
    assert grid_device.load_registry(args.state) == {}


def test_bundle_from_add_is_readable_by_join(tmp_path):
    args = make_args(tmp_path)
    with coordinator([]) as client:
        grid_device.add(args, client, "http://127.0.0.1:8765", "a")
    path = args.state / "laptop.env"
    if not honors_chmod(tmp_path):
        pytest.skip("file system ignores chmod (e.g. fuseblk)")
    bundle = grid_join.read_bundle(path)
    assert bundle["CAG_TUNNEL_USER"] == "gridtunnel" and bundle["CAG_TUNNEL_LOCAL_PORT"] == "8765"


def write_minimal_bundle(path):
    path.write_text(
        "CAG_TUNNEL_HOST=coordinator.example\nCAG_TUNNEL_USER=gridtunnel\n"
        "CAG_TUNNEL_HOST_KEY=ssh-ed25519 AAAA\nCAG_WORKER_TOKEN=cag_x\nCAG_MODEL=demo-model\n")


@pytest.mark.skipif(os.name != "nt", reason="Windows-only ACL check")
def test_windows_acl_check_accepts_owner_only_file(tmp_path):
    bundle_path = tmp_path / "worker.env"
    write_minimal_bundle(bundle_path)
    subprocess.run(["icacls", str(bundle_path), "/inheritance:r"], check=True, capture_output=True)
    subprocess.run(["icacls", str(bundle_path), "/grant:r", f"{os.environ['USERNAME']}:(R,W)"],
                   check=True, capture_output=True)
    bundle = grid_join.read_bundle(bundle_path)
    assert bundle["CAG_TUNNEL_USER"] == "gridtunnel"


@pytest.mark.skipif(os.name != "nt", reason="Windows-only ACL check")
def test_windows_acl_check_rejects_file_accessible_to_others(tmp_path):
    bundle_path = tmp_path / "worker.env"
    write_minimal_bundle(bundle_path)
    subprocess.run(["icacls", str(bundle_path), "/inheritance:r"], check=True, capture_output=True)
    subprocess.run(["icacls", str(bundle_path), "/grant:r", "*S-1-1-0:(R)"], check=True, capture_output=True)
    with pytest.raises(ValueError, match="accessible"):
        grid_join.read_bundle(bundle_path)


def test_worker_env_points_status_path_at_config_dir(tmp_path):
    bundle = {"CAG_TUNNEL_HOST": "coordinator.example", "CAG_TUNNEL_LOCAL_PORT": "8765",
              "CAG_WORKER_TOKEN": "cag_synthetic", "CAG_MODEL": "demo-model"}
    env = grid_join.worker_env(bundle, {"CAG_OLD": "stale", "PATH": "/usr/bin"}, tmp_path)
    assert env["CAG_STATUS_PATH"] == str(tmp_path / "status.json")
    assert env["CAG_COORD_URL"] == "http://127.0.0.1:8765"
    assert "CAG_OLD" not in env and env["PATH"] == "/usr/bin"


def test_coordinator_settings_prefer_environment_then_file(tmp_path):
    path = tmp_path / "coordinator.env"
    path.write_text("# comment\nCAG_ADMIN_KEY=from-file\nCAG_PORT=9100\n")
    assert grid_device.coordinator_settings({}, path) == ("from-file", "http://127.0.0.1:9100")
    assert grid_device.coordinator_settings({"CAG_ADMIN_KEY": "env", "CAG_COORD_URL": "http://127.0.0.1:1/"}, path) == (
        "env", "http://127.0.0.1:1")
    assert grid_device.coordinator_settings({}, tmp_path / "missing") == ("", "http://127.0.0.1:8765")
