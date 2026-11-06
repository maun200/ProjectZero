"""Per-user configuration for the ProjectZero client.

The file holds the user token and the machine's worker token, so it is written with
owner-only permissions (POSIX) and never leaves this computer.
"""
import ipaddress
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_SETTINGS = {
    "contribute": False,      # share this machine's compute with the grid
    "backend": "ollama",      # ollama | mock
    "cpu_threads": 2,
    "gpu_layers": 0,
    "max_output_tokens": 256,
    "context_tokens": 2048,
}
LIMITS = {"cpu_threads": (1, 8), "gpu_layers": (0, 999), "max_output_tokens": (16, 512),
          "context_tokens": (512, 8192)}


def config_dir() -> Path:
    override = os.environ.get("PROJECTZERO_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "ProjectZero"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ProjectZero"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "projectzero"


def is_private_host(host: str) -> bool:
    """Loopback, RFC 1918, link-local, CGNAT/overlay (e.g. Tailscale) or a .local/.lan/ts.net name."""
    if not host:
        return False
    if host in {"localhost"} or host.endswith((".local", ".lan", ".home.arpa", ".ts.net")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (address.is_private or address.is_loopback or address.is_link_local
            or address in ipaddress.ip_network("100.64.0.0/10"))


def check_coordinator_url(url: str) -> str:
    """Accept https anywhere, plain http only for loopback/private-network coordinators."""
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Enter an address like https://grid.example.org or http://192.168.1.10:8765")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("The address must not contain credentials, query parameters or fragments")
    if parts.scheme == "http" and not is_private_host(parts.hostname):
        raise ValueError("Plain http is only allowed inside your own network (LAN/VPN). Use https for public servers.")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def clamp_settings(values: dict) -> dict:
    out = dict(DEFAULT_SETTINGS)
    for key, default in DEFAULT_SETTINGS.items():
        if key not in values:
            continue
        value = values[key]
        if key == "contribute":
            out[key] = bool(value)
        elif key == "backend":
            out[key] = value if value in {"ollama", "mock"} else default
        else:
            low, high = LIMITS[key]
            try:
                out[key] = max(low, min(high, int(value)))
            except (TypeError, ValueError):
                pass
    return out


class Config:
    def __init__(self, directory: Path | None = None):
        self.dir = directory or config_dir()
        self.path = self.dir / "config.json"
        self.data = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                data["settings"] = clamp_settings(data.get("settings") or {})
                return data
        except (OSError, ValueError):
            pass
        return {"settings": dict(DEFAULT_SETTINGS)}

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.dir, 0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(self.data, handle, indent=2)
        os.replace(tmp, self.path)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def update(self, **values) -> None:
        self.data.update(values)
        self.save()

    def forget_account(self) -> None:
        settings = self.data.get("settings", dict(DEFAULT_SETTINGS))
        self.data = {"settings": {**settings, "contribute": False}}
        self.save()
