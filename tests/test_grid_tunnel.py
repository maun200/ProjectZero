"""Real OpenSSH check of the tunnel-only key: a private, unprivileged sshd on loopback (never the system sshd)."""
import getpass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import grid_device  # noqa: E402
import grid_join  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SSHD = shutil.which("sshd") or "/usr/sbin/sshd"
pytestmark = pytest.mark.skipif(not (os.path.exists(SSHD) and shutil.which("ssh") and shutil.which("ssh-keygen")
                                     and os.name == "posix"), reason="needs OpenSSH client and server binaries")


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *args):
        pass


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def keygen(path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(path)], check=True)
    return path.with_suffix(".pub").read_text()


@pytest.fixture
def lab(tmp_path):
    (tmp_path / "probe").write_text("")
    (tmp_path / "probe").chmod(0o600)
    if (tmp_path / "probe").stat().st_mode & 0o777 != 0o600:
        pytest.skip("file system ignores chmod; ssh refuses such private keys")
    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Health) for _ in range(2)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    allowed, forbidden = (server.server_address[1] for server in servers)
    host_pub = keygen(tmp_path / "host_key")
    config = tmp_path / "device"
    config.mkdir(mode=0o700)
    device_pub = keygen(config / "id_ed25519")
    key_type, key_b64 = grid_device.parse_public_key(device_pub)
    (tmp_path / "authorized_keys").write_text(grid_device.key_line("lab", "w1", key_type, key_b64, allowed) + "\n")
    ssh_port = free_port()
    (tmp_path / "sshd_config").write_text("\n".join([
        f"Port {ssh_port}", "ListenAddress 127.0.0.1", f"HostKey {tmp_path / 'host_key'}",
        f"AuthorizedKeysFile {tmp_path / 'authorized_keys'}", "UsePAM no", "StrictModes no", "PidFile none",
        "PasswordAuthentication no", "KbdInteractiveAuthentication no", "LogLevel ERROR", ""]))
    sshd = subprocess.Popen([SSHD, "-D", "-e", "-f", str(tmp_path / "sshd_config")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", ssh_port)) == 0:
                break
        time.sleep(0.1)
    else:
        sshd.kill()
        pytest.skip("unprivileged sshd did not start")
    bundle = {"CAG_TUNNEL_HOST": "127.0.0.1", "CAG_TUNNEL_PORT": str(ssh_port), "CAG_TUNNEL_USER": getpass.getuser(),
              "CAG_TUNNEL_HOST_KEY": " ".join(host_pub.split()[:2]), "CAG_TUNNEL_REMOTE_PORT": str(allowed),
              "CAG_TUNNEL_LOCAL_PORT": str(free_port()), "CAG_WORKER_TOKEN": "cag_synthetic", "CAG_MODEL": "demo-model"}
    (config / "known_hosts").write_text(f"{grid_join.ALIAS} {bundle['CAG_TUNNEL_HOST_KEY']}\n")
    yield {"tmp": tmp_path, "config": config, "bundle": bundle, "forbidden": forbidden}
    sshd.terminate()
    sshd.wait(timeout=5)
    for server in servers:
        server.shutdown()


def write_bundle(lab, **overrides):
    path = lab["config"] / "worker.env"
    path.write_text("".join(f"{key}={value}\n" for key, value in dict(lab["bundle"], **overrides).items()))
    path.chmod(0o600)
    return path


def check(lab):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "grid_join.py"), "check", "--config", str(lab["config"])],
                          capture_output=True, text=True, timeout=60)


def test_check_reaches_coordinator_through_tunnel(lab):
    write_bundle(lab)
    result = check(lab)
    assert result.returncode == 0, result.stderr
    assert "/health OK" in result.stderr


def test_pinned_host_key_mismatch_is_refused(lab):
    other = keygen(lab["tmp"] / "other_host_key")
    write_bundle(lab, CAG_TUNNEL_HOST_KEY=" ".join(other.split()[:2]))
    assert check(lab).returncode == 1


def tunnel_to(lab, remote_port, local_port):
    bundle = dict(lab["bundle"], CAG_TUNNEL_REMOTE_PORT=str(remote_port), CAG_TUNNEL_LOCAL_PORT=str(local_port))
    command = grid_join.ssh_command(bundle, lab["config"] / "id_ed25519", lab["config"] / "known_hosts")
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_forward_to_any_other_port_is_denied(lab):
    local = free_port()
    tunnel = tunnel_to(lab, lab["forbidden"], local)
    try:
        time.sleep(1.5)
        with pytest.raises(httpx.HTTPError):
            httpx.get(f"http://127.0.0.1:{local}/health", timeout=3, trust_env=False)
    finally:
        tunnel.terminate()
        tunnel.wait(timeout=5)


def test_key_cannot_run_commands(lab):
    marker = lab["tmp"] / "should-not-exist"
    command = grid_join.ssh_command(lab["bundle"], lab["config"] / "id_ed25519", lab["config"] / "known_hosts")
    command = [part for part in command if part not in ("-N", "-L")]
    command.remove(next(part for part in command if part.startswith("127.0.0.1:")))
    result = subprocess.run(command + [f"touch {marker}"], capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert not marker.exists()
