"""Read-only hardware inventory of a prospective worker device, with a starting configuration.

    python3 scripts/device_inventory.py              # standard library only; runs before the venv exists

Prints JSON: OS, Python, CPU (model, cores, SIMD flags), memory, GPU, free disk and a local Ollama
if one is listening on loopback (approved models by name, others only counted). No hostnames, IP/MAC addresses or serial numbers are collected.
The recommendation is a starting point derived from the 2026-11-03 measurements; confirm it with
scripts/bench_ollama.py on the device. Nothing is installed, downloaded or changed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import urllib.request

FLAGS = ("sse4_2", "avx", "avx2", "fma", "f16c", "avx512f", "avx_vnni")
SMALL, LARGE = "qwen2.5-coder:1.5b", "qwen2.5-coder:7b-instruct"


def parse_cpuinfo(text: str) -> dict:
    model, flags, cores = None, set(), set()
    physical = core = None
    for line in text.splitlines():
        key, _, value = (part.strip() for part in line.partition(":"))
        if key == "model name" and model is None:
            model = value
        elif key == "flags" and not flags:
            flags = set(value.split())
        elif key == "physical id":
            physical = value
        elif key == "core id":
            core = value
        elif not key and physical is not None and core is not None:
            cores.add((physical, core))
            physical = core = None
    if physical is not None and core is not None:
        cores.add((physical, core))
    return {"model": model, "physical_cores": len(cores) or None, "flags": sorted(f for f in FLAGS if f in flags)}


def parse_meminfo(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if rest.strip().endswith("kB"):
            values[key] = int(rest.split()[0]) * 1024
    gib = lambda key: round(values[key] / 2**30, 1) if key in values else None
    return {"total_gib": gib("MemTotal"), "available_gib": gib("MemAvailable"), "swap_gib": gib("SwapTotal")}


def run(command: list[str]) -> str | None:
    if not shutil.which(command[0]):
        return None
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def gpus() -> list[dict]:
    found = []
    smi = run(["nvidia-smi", "--query-gpu=name,memory.total,compute_cap,driver_version",
               "--format=csv,noheader,nounits"])
    for line in (smi or "").strip().splitlines():
        name, memory, capability, driver = (part.strip() for part in line.split(","))
        found.append({"vendor": "nvidia", "name": name, "vram_gib": round(int(memory) / 1024, 1),
                      "compute_capability": capability, "driver": driver})
    if found:
        return found
    for line in (run(["lspci"]) or "").splitlines():
        if any(kind in line for kind in ("VGA compatible controller", "3D controller", "Display controller")):
            # Drop the PCI bus address; keep only the device description.
            found.append({"vendor": "unknown", "name": line.split(": ", 1)[-1].strip(), "vram_gib": None})
    return found


def ollama() -> dict | None:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1) as response:
            tags = json.load(response)
        with urllib.request.urlopen("http://127.0.0.1:11434/api/version", timeout=1) as response:
            version = json.load(response).get("version")
    except (OSError, ValueError):
        return {"installed": shutil.which("ollama") is not None, "listening": False}
    names = {model["name"] for model in tags.get("models", [])}
    # Only operator-approved models are named; anything else installed is reported as a count.
    return {"installed": True, "listening": True, "version": version,
            "approved_models": sorted(names & {SMALL, LARGE}), "other_models_count": len(names - {SMALL, LARGE})}


def recommend(inventory: dict) -> dict:
    cpu, memory = inventory["cpu"], inventory["memory"]
    total = memory["total_gib"] or 0
    cores = cpu["physical_cores"] or inventory["cpu"]["logical"] or 1
    notes = []
    if "avx2" not in cpu["flags"]:
        notes.append("No AVX2: CPU inference will be much slower; test Ollama on this CPU before relying on it.")
    gpu = next((g for g in inventory["gpus"] if g["vendor"] == "nvidia" and g["vram_gib"]), None)
    if gpu and gpu["vram_gib"] >= 5.5:
        model, offload = LARGE, "num_gpu 99"
    elif gpu and gpu["vram_gib"] >= 2:
        model, offload = SMALL, "num_gpu 99"
    else:
        offload = "none (CPU)"
        model = LARGE if total >= 14 and "avx2" in cpu["flags"] else SMALL if total >= 3.5 else None
    if gpu:
        notes.append("GPU offload needs the operator's approval; check that this GPU's compute capability is "
                     "supported by the installed Ollama version.")
    if model is None:
        notes.append("Less than ~4 GiB RAM: not suitable for the approved models.")
    if model == LARGE and offload.startswith("none"):
        notes.append("7B on CPU measured ~7.7 tok/s on an i9-9900K; prefer 1.5B for responsiveness.")
    notes.append("Leave one core free (num_thread - 1) if a person uses this device at the same time.")
    return {"model": model, "gpu_offload": offload, "num_thread": max(1, min(cores, 6)),
            "num_ctx": 2048 if total >= 6 else 1024, "notes": notes,
            "reference_1.5b_cpu_tok_s": {"i9-9900K 4 threads": 32.7, "i5 10th gen laptop 4 threads": 17.7}}


def collect() -> dict:
    os_release = {}
    if Path("/etc/os-release").exists():
        for line in Path("/etc/os-release").read_text().splitlines():
            key, _, value = line.partition("=")
            os_release[key] = value.strip('"')
    read = lambda path: Path(path).read_text() if Path(path).exists() else ""
    cpu = parse_cpuinfo(read("/proc/cpuinfo"))
    cpu["model"] = cpu["model"] or platform.processor() or None
    cpu["logical"] = os.cpu_count()
    inventory = {
        "os": {"system": platform.system(), "distribution": os_release.get("PRETTY_NAME"), "kernel": platform.release(),
               "architecture": platform.machine()},
        "python": platform.python_version(),
        "python_ok_for_worker": sys.version_info >= (3, 11),
        "cpu": cpu,
        "memory": parse_meminfo(read("/proc/meminfo")),
        "gpus": gpus(),
        "disk_free_gib": round(shutil.disk_usage(Path.home()).free / 2**30, 1),
        "ollama": ollama(),
    }
    inventory["recommendation"] = recommend(inventory)
    return inventory


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2))
