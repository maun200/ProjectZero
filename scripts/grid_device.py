"""Coordinator-host helper: give one device a worker token and a tunnel-only SSH key.

    python scripts/grid_device.py add laptop --pubkey laptop.pub --host <address> --model qwen2.5-coder:1.5b
    python scripts/grid_device.py remove laptop
    python scripts/grid_device.py list

The admin key and port come from CAG_ADMIN_KEY / CAG_COORD_URL or, if unset, from the file that
scripts/run_coordinator.sh creates (~/.config/community-grid/coordinator.env).

The SSH key may only open a forward to the coordinator port: no shell, no other ports.
The device bundle contains the worker token; it is written with mode 0600 and must be
moved to the device privately (e.g. USB stick or an already trusted channel).
Run scripts/grid_host_setup.sh once (with sudo) before the first `add`.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import httpx

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}")
KEY_TYPES = {"ssh-ed25519", "sk-ssh-ed25519@openssh.com", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
             "ecdsa-sha2-nistp521", "sk-ecdsa-sha2-nistp256@openssh.com"}
MARKER = "cag-device:"
DEFAULT_KEYS = Path("/etc/ssh/community-grid/authorized_keys")
DEFAULT_HOST_KEY = Path("/etc/ssh/ssh_host_ed25519_key.pub")
DEFAULT_STATE = Path.home() / ".config" / "community-grid" / "devices"
DEFAULT_COORDINATOR_ENV = Path.home() / ".config" / "community-grid" / "coordinator.env"


def parse_public_key(text: str) -> tuple[str, str]:
    """Return (type, base64) of exactly one OpenSSH public key; options prefixes are rejected."""
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError("Expected exactly one public key line")
    parts = lines[0].split()
    if len(parts) < 2 or parts[0] not in KEY_TYPES:
        raise ValueError("Unsupported key type or options prefix; use an ed25519 or ecdsa public key")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError:
        raise ValueError("Public key is not valid base64") from None
    length = int.from_bytes(blob[:4], "big")
    if blob[4:4 + length] != parts[0].encode():
        raise ValueError("Public key data does not match its type")
    return parts[0], parts[1]


def key_line(name: str, worker_id: str, key_type: str, key_b64: str, coordinator_port: int) -> str:
    # `restrict` disables pty, agent/X11 forwarding and rc files; only this one forward is re-allowed.
    return (f'restrict,port-forwarding,permitopen="127.0.0.1:{coordinator_port}",command="/usr/sbin/nologin" '
            f"{key_type} {key_b64} {MARKER}{name}:{worker_id}")


def without_device(content: str, name: str) -> str:
    kept = [line for line in content.splitlines() if not line.rstrip().split(" ")[-1].startswith(f"{MARKER}{name}:")]
    return "\n".join(kept) + ("\n" if kept else "")


def write_keys(path: Path, content: str) -> None:
    if os.access(path, os.W_OK):
        path.write_text(content)
        return
    print(f"Updating {path} with sudo", file=sys.stderr)
    subprocess.run(["sudo", "tee", str(path)], input=content, text=True, stdout=subprocess.DEVNULL, check=True)


def write_private(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(content)


def bundle(name: str, host: str, ssh_port: int, tunnel_user: str, host_key: str,
           coordinator_port: int, worker_token: str, model: str) -> str:
    return "\n".join([
        f"# Community AI Grid device bundle for {name}. Contains a secret worker token: keep it private (0600).",
        "# Copy to the device as ~/.config/community-grid/worker.env; device-local settings may be edited.",
        f"CAG_DEVICE_NAME={name}",
        f"CAG_TUNNEL_HOST={host}",
        f"CAG_TUNNEL_PORT={ssh_port}",
        f"CAG_TUNNEL_USER={tunnel_user}",
        f"CAG_TUNNEL_HOST_KEY={host_key}",
        f"CAG_TUNNEL_REMOTE_PORT={coordinator_port}",
        f"CAG_TUNNEL_LOCAL_PORT={coordinator_port}",
        f"CAG_WORKER_TOKEN={worker_token}",
        f"CAG_MODEL={model}",
        "CAG_LLM_BACKEND=ollama",
        "CAG_LLM_URL=http://127.0.0.1:11434/api/chat",
        "CAG_OLLAMA_NUM_CTX=2048",
        "CAG_OLLAMA_NUM_THREAD=4",
        "CAG_OLLAMA_MAX_OUTPUT_TOKENS=256",
        "# GPU layers (0-999); set >0 only after the operator approved GPU use on this device.",
        "CAG_OLLAMA_NUM_GPU=0",
        "",
    ])


def coordinator_settings(environ: dict, path: Path) -> tuple[str, str]:
    """Return (admin key, base URL): environment first, then run_coordinator.sh's file."""
    stored = {}
    if path.exists():
        for line in path.read_text().splitlines():
            name, sep, value = line.strip().partition("=")
            if sep and not name.startswith("#"):
                stored[name] = value.strip()
    key = environ.get("CAG_ADMIN_KEY") or stored.get("CAG_ADMIN_KEY", "")
    base = environ.get("CAG_COORD_URL") or f"http://127.0.0.1:{stored.get('CAG_PORT', '8765')}"
    return key, base.rstrip("/")


def load_registry(state: Path) -> dict:
    path = state / "registry.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save_registry(state: Path, registry: dict) -> None:
    (state / "registry.json").write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n")


def add(args, client: httpx.Client, base: str, admin_key: str) -> dict:
    if not NAME.fullmatch(args.name):
        raise ValueError("Device name: 1-32 characters a-z, 0-9 and '-'")
    if not HOST.fullmatch(args.host):
        raise ValueError("--host must be a plain host name or IP address")
    key_type, key_b64 = parse_public_key(Path(args.pubkey).read_text())
    host_key = " ".join(args.host_key.read_text().split()[:2])
    parse_public_key(host_key)
    if not args.authorized_keys.exists():
        raise ValueError(f"{args.authorized_keys} is missing; run scripts/grid_host_setup.sh with sudo first")
    current = args.authorized_keys.read_text()
    registry = load_registry(args.state)
    if args.name in registry or f"{MARKER}{args.name}:" in current:
        raise ValueError(f"Device {args.name!r} already exists; remove it first")
    if key_b64 in current:
        raise ValueError("This SSH key is already registered for another device")
    bundle_path = args.state / f"{args.name}.env"
    if bundle_path.exists():
        raise ValueError(f"{bundle_path} already exists")
    headers = {"Authorization": f"Bearer {admin_key}"}
    response = client.post(f"{base}/api/admin/workers", headers=headers,
                           json={"name": args.name, "model": args.model, "trust_level": args.trust})
    response.raise_for_status()
    worker = response.json()
    try:
        line = key_line(args.name, worker["id"], key_type, key_b64, args.coordinator_port)
        write_keys(args.authorized_keys, current + ("" if current.endswith("\n") or not current else "\n") + line + "\n")
        write_private(bundle_path, bundle(args.name, args.host, args.ssh_port, args.tunnel_user, host_key,
                                          args.coordinator_port, worker["worker_token"], args.model))
    except Exception:
        client.post(f"{base}/api/admin/workers/{worker['id']}/revoke", headers=headers)
        raise
    registry[args.name] = {"worker_id": worker["id"], "model": args.model, "trust_level": args.trust}
    save_registry(args.state, registry)
    return {"device": args.name, "worker_id": worker["id"], "bundle": str(bundle_path)}


def remove(args, client: httpx.Client, base: str, admin_key: str) -> dict:
    registry = load_registry(args.state)
    if args.name not in registry:
        raise ValueError(f"Unknown device {args.name!r}")
    # Removing the key blocks new tunnels; revoking the token also stops a tunnel that is still open.
    if args.authorized_keys.exists():
        write_keys(args.authorized_keys, without_device(args.authorized_keys.read_text(), args.name))
    worker_id = registry[args.name]["worker_id"]
    response = client.post(f"{base}/api/admin/workers/{worker_id}/revoke",
                           headers={"Authorization": f"Bearer {admin_key}"})
    if response.status_code != 404:
        response.raise_for_status()
    (args.state / f"{args.name}.env").unlink(missing_ok=True)
    del registry[args.name]
    save_registry(args.state, registry)
    return {"device": args.name, "worker_id": worker_id, "key_removed": True,
            "revoked": response.status_code != 404}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help="Where bundles and the registry live")
    parser.add_argument("--authorized-keys", type=Path, default=DEFAULT_KEYS)
    commands = parser.add_subparsers(dest="command", required=True)
    adding = commands.add_parser("add", help="Enroll a device and allow its tunnel key")
    adding.add_argument("name")
    adding.add_argument("--pubkey", required=True, help="The device's public key (from grid_join.py init)")
    adding.add_argument("--host", required=True, help="Address the device uses to reach this host's SSH server")
    adding.add_argument("--ssh-port", type=int, default=22)
    adding.add_argument("--tunnel-user", default="gridtunnel")
    adding.add_argument("--host-key", type=Path, default=DEFAULT_HOST_KEY)
    adding.add_argument("--model", required=True, help="Exact operator-approved model tag for this device")
    adding.add_argument("--trust", choices=["trusted", "community"], default="trusted")
    removing = commands.add_parser("remove", help="Remove the tunnel key and revoke the worker token")
    removing.add_argument("name")
    commands.add_parser("list", help="Show registered devices (no secrets)")
    args = parser.parse_args()
    if args.command == "list":
        print(json.dumps(load_registry(args.state), indent=2, sort_keys=True))
        return
    key, base = coordinator_settings(dict(os.environ), DEFAULT_COORDINATOR_ENV)
    if not key:
        sys.exit("Set CAG_ADMIN_KEY or start the coordinator once with scripts/run_coordinator.sh")
    args.coordinator_port = httpx.URL(base).port or 8765
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with httpx.Client(timeout=15, trust_env=False) as client:
        try:
            result = (add if args.command == "add" else remove)(args, client, base, key)
        except ValueError as exc:
            sys.exit(f"Error: {exc}")
    print(json.dumps(result, indent=2))
    if args.command == "add":
        print("IMPORTANT: The bundle contains the worker token. Move it to the device privately, then delete it here.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
