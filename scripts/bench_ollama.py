"""Streaming benchmark of one already installed, operator-approved Ollama model on loopback.

Measures cold load, time to first token, token rates and wall time over fixed synthetic
prompts, plus CPU/GPU temperature samples. Never downloads weights; unloads the model at
the end. Response texts are only used for the arithmetic check and never stored.

    python scripts/bench_ollama.py --model qwen2.5-coder:1.5b --runs 10 --num-thread 2
"""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time

import httpx

from bench_lib import parse_stream, read_cpu_temp_c, sample_gpu, summarize
from ollama_preflight import check_backend

ROOT = Path(__file__).resolve().parent.parent
OLLAMA = "http://127.0.0.1:11434"
PROMPTS = [
    "What is 2 + 2? Reply with the digit only.",
    "Write a Python function is_even(n) that returns True for even integers. Code only.",
    "Summarize in one sentence: Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do "
    "eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis "
    "nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat.",
]


def bounded(kind, low, high):
    def parse(text):
        value = kind(text)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return value
    return parse


def run_once(client: httpx.Client, args, prompt: str, keep_alive):
    started = time.monotonic()
    with client.stream("POST", OLLAMA + "/api/chat", json={
            "model": args.model, "stream": True, "keep_alive": keep_alive,
            "messages": [{"role": "user", "content": prompt}],
            "options": {"num_ctx": args.num_ctx, "num_thread": args.num_thread, "num_predict": args.max_tokens,
                        "num_gpu": args.num_gpu, "num_batch": 32, "temperature": 0, "seed": 42}}) as response:
        response.raise_for_status()
        return parse_stream(((time.monotonic(), line) for line in response.iter_lines()), started)


def public(result) -> dict:
    data = asdict(result)
    data.pop("text")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, help="Exact installed, operator-approved model tag")
    parser.add_argument("--runs", type=bounded(int, 1, 50), default=10)
    parser.add_argument("--num-thread", type=bounded(int, 1, 8), default=2)
    parser.add_argument("--num-ctx", type=bounded(int, 256, 8192), default=1024)
    parser.add_argument("--max-tokens", type=bounded(int, 1, 512), default=64)
    parser.add_argument("--num-gpu", type=bounded(int, 0, 999), default=0)
    parser.add_argument("--i-approve-gpu", action="store_true", help="Required for --num-gpu > 0")
    parser.add_argument("--min-free-gib", type=float, default=3.0)
    args = parser.parse_args()
    if args.num_gpu and not args.i_approve_gpu:
        parser.error("--num-gpu > 0 needs explicit operator approval (--i-approve-gpu)")

    with httpx.Client(timeout=httpx.Timeout(600, connect=10), trust_env=False) as client:
        installed = check_backend(client, args.model, args.min_free_gib, OLLAMA)
        version = client.get(OLLAMA + "/api/version").json().get("version")
        samples = [{"phase": "before", "cpu_temp_c": read_cpu_temp_c(), "gpu": sample_gpu()}]
        try:
            cold = run_once(client, args, PROMPTS[0], "2m")
            arithmetic = cold.text.strip() == "4"
            warm = []
            for i in range(args.runs):
                result = run_once(client, args, PROMPTS[i % len(PROMPTS)], "2m")
                if i % len(PROMPTS) == 0:
                    arithmetic = arithmetic and result.text.strip() == "4"
                warm.append(result)
                samples.append({"phase": f"after_run_{i + 1}", "cpu_temp_c": read_cpu_temp_c(), "gpu": sample_gpu()})
        finally:
            client.post(OLLAMA + "/api/generate", json={"model": args.model, "keep_alive": 0})
            for _ in range(20):
                if not client.get(OLLAMA + "/api/ps").json().get("models"):
                    break
                time.sleep(0.5)
        loaded_after = len(client.get(OLLAMA + "/api/ps").json().get("models", []))

    cpuinfo = Path("/proc/cpuinfo").read_text()
    meminfo = Path("/proc/meminfo").read_text()
    report = {
        "label": "single-machine small-sample benchmark; fixed synthetic prompts",
        "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": args.model, "model_size_bytes": installed.get("size"),
        "quantization": installed.get("details", {}).get("quantization_level"), "ollama_version": version,
        "options": {"num_ctx": args.num_ctx, "num_thread": args.num_thread, "num_predict": args.max_tokens,
                    "num_gpu": args.num_gpu, "num_batch": 32, "temperature": 0, "seed": 42},
        "cpu": next((l.split(":", 1)[1].strip() for l in cpuinfo.splitlines() if l.startswith("model name")), None),
        "mem_total_kib": int(re.search(r"MemTotal:\s+(\d+)", meminfo).group(1)),
        "cold_run": public(cold), "warm_summary": summarize(warm), "warm_runs": [public(r) for r in warm],
        "samples": samples, "arithmetic_check": "PASS" if arithmetic else "FAIL",
        "models_loaded_after": loaded_after,
    }
    out_dir = ROOT / ".pytest_cache" / "bench"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{stamp}-{re.sub(r'[^A-Za-z0-9.-]', '_', args.model)}-t{args.num_thread}-g{args.num_gpu}.json"
    path.write_text(json.dumps(report, indent=2))
    print(json.dumps({"file": str(path.relative_to(ROOT)), "cold_load_s": round(cold.load_s, 2),
                      "cold_ttft_s": cold.ttft_s and round(cold.ttft_s, 2), **report["warm_summary"],
                      "arithmetic_check": report["arithmetic_check"], "models_loaded_after": loaded_after}, indent=2))


if __name__ == "__main__":
    main()
