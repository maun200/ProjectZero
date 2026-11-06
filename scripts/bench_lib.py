"""Pure helpers for scripts/bench_ollama.py (standard library only, unit-tested)."""
from dataclasses import dataclass
import json
import math
from pathlib import Path
import shutil
import subprocess
from typing import Iterable, Sequence


@dataclass
class RunResult:
    ttft_s: float | None
    wall_s: float
    load_s: float
    prompt_tokens: int
    output_tokens: int
    prompt_tok_per_s: float | None
    output_tok_per_s: float | None
    done_reason: str | None
    text: str  # used only for the answer check; never written to result files


def rate(count: int, nanoseconds: int) -> float | None:
    return count / (nanoseconds / 1e9) if nanoseconds > 0 else None


def parse_stream(lines: Iterable[tuple[float, str]], started: float) -> RunResult:
    """Turn (receive time, NDJSON line) pairs of an Ollama /api/chat stream into one result."""
    ttft, last, parts, final = None, started, [], None
    for received, line in lines:
        if not line.strip():
            continue
        chunk = json.loads(line)
        last = received
        content = chunk.get("message", {}).get("content", "")
        if content:
            parts.append(content)
            if ttft is None:
                ttft = received - started
        if chunk.get("done"):
            final = chunk
    if final is None:
        raise ValueError("stream ended without a done chunk")
    prompt_tokens, output_tokens = final.get("prompt_eval_count", 0), final.get("eval_count", 0)
    return RunResult(ttft_s=ttft, wall_s=last - started, load_s=final.get("load_duration", 0) / 1e9,
                     prompt_tokens=prompt_tokens, output_tokens=output_tokens,
                     prompt_tok_per_s=rate(prompt_tokens, final.get("prompt_eval_duration", 0)),
                     output_tok_per_s=rate(output_tokens, final.get("eval_duration", 0)),
                     done_reason=final.get("done_reason"), text="".join(parts))


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile; with small n this is coarse, report n alongside."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q / 100 * len(ordered))) - 1]


def summarize(runs: Sequence[RunResult]) -> dict:
    def pick(field: str, q: float):
        values = [getattr(r, field) for r in runs if getattr(r, field) is not None]
        return percentile(values, q) if values else None
    return {"n": len(runs), "ttft_p50": pick("ttft_s", 50), "ttft_p95": pick("ttft_s", 95),
            "wall_p50": pick("wall_s", 50), "wall_p95": pick("wall_s", 95),
            "output_tok_per_s_p50": pick("output_tok_per_s", 50),
            "prompt_tok_per_s_p50": pick("prompt_tok_per_s", 50)}


def read_cpu_temp_c() -> float | None:
    temps = []
    for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
        try:
            if (zone / "type").read_text().strip() == "x86_pkg_temp":
                temps.append(int((zone / "temp").read_text()) / 1000)
        except (OSError, ValueError):
            continue
    return max(temps) if temps else None


def sample_gpu() -> dict | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu,power.draw,utilization.gpu,memory.used",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5, check=True)
        temp, power, util, memory = [v.strip() for v in out.stdout.splitlines()[0].split(",")]
        return {"temp_c": float(temp), "power_w": float(power), "util_pct": float(util), "memory_mib": float(memory)}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
