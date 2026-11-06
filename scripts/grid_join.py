"""Worker-device helper: keep a tunnel-only SSH connection to the coordinator and run the worker.

    python scripts/grid_join.py init     # once: create this device's SSH key and print the public key
    python scripts/grid_join.py check    # open the tunnel, check the coordinator's /health, close
    python scripts/grid_join.py run      # tunnel + worker; reconnects the tunnel; Ctrl+C stops both

Reads ~/.config/community-grid/worker.env (the bundle from grid_device.py add). The coordinator's
host key is pinned from the bundle; nothing is accepted on first use. Never runs remote commands.
"""
import argparse
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = Path.home() / ".config" / "community-grid"
# A console-less parent (e.g. a GUI launching this headless) makes Windows allocate a new,
# briefly visible console for each child here unless told not to; harmless to always pass
# this on Windows, including from an interactive terminal (inherited I/O still works).
POPEN_KWARGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
ALIAS = "community-grid-coordinator"
REQUIRED = ("CAG_TUNNEL_HOST", "CAG_TUNNEL_USER", "CAG_TUNNEL_HOST_KEY", "CAG_WORKER_TOKEN", "CAG_MODEL")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}")
USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
LINE = re.compile(r"([A-Z][A-Z0-9_]*)=(.*)")


def _windows_acl_principals(path: Path) -> list[str]:
    """Best-effort, locale-independent by construction: we only ever compare each
    listed principal against the current account, never against a localized group
    name, so it does not matter whether icacls prints e.g. "Everyone" or "Jeder"."""
    result = subprocess.run(["icacls", str(path)], capture_output=True, text=True, timeout=10)
    principals = []
    prefix = f"{path} "
    for raw in result.stdout.splitlines():
        line = raw[len(prefix):] if raw.startswith(prefix) else raw.strip()
        line = line.strip()
        if not line or ":(" not in line:
            continue
        principals.append(line.split(":(", 1)[0].strip().upper())
    return principals


def _windows_bundle_too_permissive(path: Path) -> bool:
    try:
        principals = _windows_acl_principals(path)
    except (OSError, subprocess.SubprocessError):
        return False  # best-effort: never block startup just because icacls could not run
    if not principals:
        return False
    username = os.environ.get("USERNAME", "").upper()
    owner = f"{os.environ.get('USERDOMAIN', '')}\\{username}".upper()
    return any(p != owner and p != username and not p.endswith(f"\\{username}") for p in principals if username)


def read_bundle(path: Path) -> dict:
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise ValueError(f"{path} is readable by other users; run: chmod 600 {path}")
    if os.name == "nt" and _windows_bundle_too_permissive(path):
        raise ValueError(f"{path} is accessible to more than your own account; restrict it, e.g.:\n"
                         f'  icacls "{path}" /inheritance:r\n'
                         f'  icacls "{path}" /grant:r "%USERNAME%:(R,W)"')
    values = {}
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = LINE.fullmatch(line)
        if not match:
            raise ValueError(f"{path}:{number}: expected KEY=VALUE")
        values[match[1]] = match[2].strip()
    missing = [key for key in REQUIRED if not values.get(key)]
    if missing:
        raise ValueError(f"{path}: missing {', '.join(missing)}")
    if not HOST.fullmatch(values["CAG_TUNNEL_HOST"]) or not USER.fullmatch(values["CAG_TUNNEL_USER"]):
        raise ValueError(f"{path}: invalid CAG_TUNNEL_HOST or CAG_TUNNEL_USER")
    for key in ("CAG_TUNNEL_PORT", "CAG_TUNNEL_LOCAL_PORT", "CAG_TUNNEL_REMOTE_PORT"):
        if not 1 <= int(values.setdefault(key, "22" if key == "CAG_TUNNEL_PORT" else "8765")) <= 65535:
            raise ValueError(f"{path}: {key} out of range")
    return values


def ssh_command(bundle: dict, key: Path, known_hosts: Path) -> list[str]:
    options = {"IdentitiesOnly": "yes", "BatchMode": "yes", "ExitOnForwardFailure": "yes",
               "ServerAliveInterval": "15", "ServerAliveCountMax": "3", "ConnectTimeout": "10",
               "StrictHostKeyChecking": "yes", "UserKnownHostsFile": str(known_hosts), "HostKeyAlias": ALIAS,
               "ForwardAgent": "no", "ForwardX11": "no", "PermitLocalCommand": "no"}
    command = ["ssh", "-F", os.devnull, "-N", "-T", "-i", str(key), "-p", bundle["CAG_TUNNEL_PORT"],
               "-L", f"127.0.0.1:{bundle['CAG_TUNNEL_LOCAL_PORT']}:127.0.0.1:{bundle['CAG_TUNNEL_REMOTE_PORT']}"]
    for name, value in options.items():
        command += ["-o", f"{name}={value}"]
    return command + ["--", f"{bundle['CAG_TUNNEL_USER']}@{bundle['CAG_TUNNEL_HOST']}"]


def worker_env(bundle: dict, base_env: dict, config: Path) -> dict:
    env = {key: value for key, value in base_env.items() if not key.startswith("CAG_")}
    env.update({key: value for key, value in bundle.items()
                if not key.startswith("CAG_TUNNEL_") and key != "CAG_DEVICE_NAME"})
    env["CAG_COORD_URL"] = f"http://127.0.0.1:{bundle['CAG_TUNNEL_LOCAL_PORT']}"
    env["CAG_STATUS_PATH"] = str(config / "status.json")
    env["CAG_LIMITS_PATH"] = str(config / "limits.json")
    return env


def wait_healthy(port: str, tunnel: subprocess.Popen, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    with httpx.Client(trust_env=False, timeout=2) as client:
        while time.monotonic() < deadline and tunnel.poll() is None:
            try:
                if client.get(f"http://127.0.0.1:{port}/health").is_success:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
    return False


def stop(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def init(config: Path) -> None:
    config.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = config / "id_ed25519"
    if not key.exists():
        # No passphrase so the tunnel can reconnect unattended; the server only allows one port forward for it.
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "community-grid", "-f", str(key)],
                       check=True)
    print((config / "id_ed25519.pub").read_text().strip())
    print("Give this public key (not id_ed25519) to the coordinator operator for grid_device.py add.",
          file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["init", "check", "run"])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Holds id_ed25519 and worker.env")
    parser.add_argument("--bundle", type=Path, help="Default: <config>/worker.env")
    parser.add_argument("--once", action="store_true", help="run: process one job, then stop")
    args = parser.parse_args()
    if args.command == "init":
        init(args.config)
        return
    try:
        bundle = read_bundle(args.bundle or args.config / "worker.env")
    except (OSError, ValueError) as exc:
        sys.exit(f"Error: {exc}")
    key = args.config / "id_ed25519"
    if not key.exists():
        sys.exit(f"Error: {key} is missing; run: python scripts/grid_join.py init")
    known_hosts = args.config / "known_hosts"
    known_hosts.write_text(f"{ALIAS} {bundle['CAG_TUNNEL_HOST_KEY']}\n")
    command = ssh_command(bundle, key, known_hosts)
    port = bundle["CAG_TUNNEL_LOCAL_PORT"]
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    tunnel = subprocess.Popen(command, stdin=subprocess.DEVNULL, **POPEN_KWARGS)
    worker = None
    try:
        healthy = wait_healthy(port, tunnel, 20)
        print(f"Tunnel {'up' if healthy else 'NOT ready'}: coordinator /health "
              f"{'OK' if healthy else 'unreachable'} via 127.0.0.1:{port}", file=sys.stderr)
        if args.command == "check":
            sys.exit(0 if healthy else 1)
        worker = subprocess.Popen([sys.executable, "-m", "worker.client", *(["--once"] if args.once else [])],
                                  cwd=ROOT, env=worker_env(bundle, dict(os.environ), args.config),
                                  stdin=subprocess.DEVNULL, **POPEN_KWARGS)
        backoff, connected_at = 2, time.monotonic()
        while worker.poll() is None:
            if tunnel.poll() is not None:
                print(f"Tunnel closed (ssh exit {tunnel.returncode}); reconnecting in {backoff} s", file=sys.stderr)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                tunnel, connected_at = (subprocess.Popen(command, stdin=subprocess.DEVNULL, **POPEN_KWARGS),
                                        time.monotonic())
            elif backoff > 2 and time.monotonic() - connected_at > 60:
                backoff = 2
            time.sleep(1)
        sys.exit(worker.returncode)
    except KeyboardInterrupt:
        sys.exit(130)
    finally:
        stop(worker)
        stop(tunnel)


if __name__ == "__main__":
    main()
