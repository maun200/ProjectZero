"""Read-only safety checks before a real-model test; never downloads or unloads anything."""
from pathlib import Path

import httpx


def check_backend(client: httpx.Client, model: str, min_free_gib: float, base: str = "http://127.0.0.1:11434",
                  meminfo: Path = Path("/proc/meminfo")) -> dict:
    tags = client.get(base + '/api/tags')
    tags.raise_for_status()
    installed = next((m for m in tags.json()['models'] if m['name'] == model), None)
    if installed is None:
        raise RuntimeError("Approved model is not installed; no download attempted")
    active = client.get(base + '/api/ps')
    active.raise_for_status()
    if active.json().get('models'):
        raise RuntimeError("Another model is loaded; leave the existing workload untouched")
    memory = dict(line.split(':', 1) for line in meminfo.read_text().splitlines())
    if int(memory['MemAvailable'].split()[0]) < min_free_gib * 1024 * 1024:
        raise RuntimeError("Not enough available RAM")
    return installed
