"""Pull-only worker with an OpenAI-compatible or bounded local Ollama endpoint.

Never executes code from the coordinator. Do not run third-party jobs with secrets.
"""
import argparse
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import time
from urllib.parse import urlsplit

import httpx


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# Keys a local GUI's resource-limit panel may override; names match Worker.__init__'s own
# limit kwargs, which already enforce the real bounds (256-8192 / 1-8 / 1-512 / 0-999) — this
# file only ever narrows or widens those same numbers, never adds a new capability.
LIMIT_OVERRIDE_KEYS = {"context_tokens", "cpu_threads", "max_output_tokens", "gpu_layers"}


def read_limits_override(path: str) -> dict:
    """Optional local overrides for Ollama resource limits (CAG_LIMITS_PATH), written by the
    worker GUI — never the worker token, never anything network-reachable. Unknown or malformed
    entries are silently ignored; Worker.__init__ still validates whatever survives."""
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if key in LIMIT_OVERRIDE_KEYS and type(value) is int}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hardware():
    """Safe, limited capacity metadata; no arbitrary command execution."""
    data = {"platform": platform.system(), "architecture": platform.machine(),
            "cpu_logical": os.cpu_count(),
            "local_implementation": "openai-compatible"}
    try:
        import psutil
        data["ram_bytes"] = psutil.virtual_memory().total
        data["cpu_percent"] = psutil.cpu_percent(interval=0.1)
    except ImportError:
        pass
    # Optional informational detection; not used to decide work eligibility.
    data["nvidia_smi_available"] = shutil.which("nvidia-smi") is not None
    return data


class LeaseLost(RuntimeError):
    """The coordinator no longer accepts this job's lease (expired, cancelled or revoked)."""


class Worker:
    def __init__(self, coordinator: str, token: str, model: str, llm_url: str,
                 llm_api_key: str = "", poll_seconds: float = 3, *,
                 backend: str = "openai", context_tokens: int = 1024,
                 cpu_threads: int = 2, max_output_tokens: int = 32, gpu_layers: int = 0,
                 lease_heartbeat_seconds: float = 30, status_path: str = ""):
        if backend not in {"openai", "ollama", "mock"}:
            raise ValueError("CAG_LLM_BACKEND must be openai, ollama or mock")
        if backend == "ollama":
            url = urlsplit(llm_url)
            if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
                    or url.username is not None or url.password is not None
                    or url.query or url.fragment or url.path != "/api/chat"):
                raise ValueError("Ollama requires an HTTP loopback URL ending in /api/chat")
            _ = url.port  # Reject malformed ports before polling the coordinator.
            for name, value, low, high in [
                ("CAG_OLLAMA_NUM_CTX", context_tokens, 256, 8192),
                ("CAG_OLLAMA_NUM_THREAD", cpu_threads, 1, 8),
                ("CAG_OLLAMA_MAX_OUTPUT_TOKENS", max_output_tokens, 1, 512),
                ("CAG_OLLAMA_NUM_GPU", gpu_layers, 0, 999),
            ]:
                if type(value) is not int or not low <= value <= high:
                    raise ValueError(f"{name} must be an integer between {low} and {high}")
        if not lease_heartbeat_seconds > 0:
            raise ValueError("CAG_LEASE_HEARTBEAT_SECONDS must be positive")
        self.lease_heartbeat_seconds = lease_heartbeat_seconds
        self.backend = backend
        self.context_tokens = context_tokens
        self.cpu_threads = cpu_threads
        self.max_output_tokens = max_output_tokens
        self.gpu_layers = gpu_layers
        self.coordinator = coordinator.rstrip("/")
        self.token = token
        self.model = model
        self.llm_url = llm_url
        self.llm_api_key = llm_api_key
        self.poll_seconds = max(1, poll_seconds)
        self.last_heartbeat = 0.0
        self.status_path = status_path
        self.transport = None  # tests only: route coordinator calls in-process

    def headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def read_status(self) -> dict:
        """Current contents of the local status file, or {} if absent/unreadable."""
        if not self.status_path:
            return {}
        try:
            return json.loads(Path(self.status_path).read_text())
        except (OSError, ValueError):
            return {}

    def write_status(self, **fields):
        """Best-effort local status file for a UI to poll; never a network interface."""
        if not self.status_path:
            return
        path = Path(self.status_path)
        try:
            current = self.read_status()
            current.update({"model": self.model, "backend": self.backend})
            current.update(fields)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(current))
            tmp.replace(path)
        except OSError as exc:
            print(f"Could not write status file: {exc}", file=sys.stderr)

    async def fetch_self_status(self, client: httpx.AsyncClient):
        """Poll the coordinator's own self-service endpoint for provisional credits/trust level,
        for a GUI to display. Uses the worker token only (same one claim/complete already use);
        never touches the separate user_token, so a compromised worker still cannot read or
        spend the linked account's settled balance."""
        try:
            response = await client.get(f"{self.coordinator}/api/worker/me", headers=self.headers())
            response.raise_for_status()
            info = response.json()
            self.write_status(provisional_credits=info.get("provisional_credits"),
                              trust_level=info.get("trust_level"))
        except Exception as exc:
            print(f"Could not fetch self status: {exc}", file=sys.stderr)

    async def heartbeat(self, client: httpx.AsyncClient, job=None):
        payload = {"capabilities": hardware()}
        payload["capabilities"]["local_implementation"] = self.backend
        if job:
            payload.update(job_id=job["id"], lease_token=job["lease_token"])
        response = await client.post(f"{self.coordinator}/api/worker/heartbeat", json=payload, headers=self.headers())
        response.raise_for_status()

    async def infer(self, job: dict) -> tuple[str, dict]:
        """Run the local model for one job; returns (content, usage). Reports nothing."""
        if job["model"] != self.model:
            raise ValueError("Job model differs from the locally approved model")
        requested = job["max_output_tokens"]
        if type(requested) is not int or requested < 1:
            raise ValueError("Job output token limit must be a positive integer")
        if self.backend == "mock":  # orchestration demo only: no model, no network
            last = next((m["content"] for m in reversed(job["messages"]) if m.get("role") == "user"), "")
            return f"[MOCK - no AI involved] You wrote: {last[:200]}", {}
        headers = {}
        if self.llm_api_key:
            headers["Authorization"] = f"Bearer {self.llm_api_key}"
        body = {"model": self.model, "messages": job["messages"],
                "max_tokens": requested, "stream": False}
        if self.backend == "ollama":
            body = {"model": self.model, "messages": job["messages"], "stream": False,
                    "keep_alive": 0, "options": {
                        "num_ctx": self.context_tokens, "num_thread": self.cpu_threads,
                        "num_predict": min(requested, self.max_output_tokens),
                        "num_gpu": self.gpu_layers, "num_batch": 32}}
        timeout = 120 if self.backend == "ollama" else 600
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15), trust_env=False) as llm:
            response = await llm.post(self.llm_url, json=body, headers=headers)
            response.raise_for_status()
            output = response.json()
        if self.backend == "ollama":
            if output.get("done") is not True:
                raise ValueError("Ollama returned an incomplete response")
            content = output.get("message", {}).get("content")
            usage = {}
            prompt, completion = output.get("prompt_eval_count"), output.get("eval_count")
            if type(prompt) is int and type(completion) is int and prompt >= 0 and completion >= 0:
                usage = {"prompt_tokens": prompt, "completion_tokens": completion,
                         "total_tokens": prompt + completion}
        else:
            content = output["choices"][0]["message"]["content"]
            usage = output.get("usage") or {}
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Model returned no text")
        return content, usage

    async def report(self, client: httpx.AsyncClient, job: dict, content: str, usage: dict):
        response = await client.post(f"{self.coordinator}/api/worker/complete", json={
            "job_id": job["id"], "lease_token": job["lease_token"], "ok": True,
            "content": content, "usage": usage}, headers=self.headers())
        response.raise_for_status()

    async def run_job(self, client: httpx.AsyncClient, job: dict):
        await self.report(client, job, *await self.infer(job))

    async def maintain_lease(self, client: httpx.AsyncClient, job: dict, stop: asyncio.Event,
                             lost: asyncio.Event):
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.lease_heartbeat_seconds)
            except asyncio.TimeoutError:
                try:
                    await self.heartbeat(client, job)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in (403, 409):  # lease gone or worker revoked
                        lost.set()
                        return
                    print(f"Lease heartbeat failed: {exc}", file=sys.stderr)
                except Exception as exc:
                    print(f"Lease heartbeat failed: {exc}", file=sys.stderr)

    async def run_until_lease_lost(self, client: httpx.AsyncClient, job: dict, lost: asyncio.Event):
        # Only the inference may be aborted. Once the report is sent, a heartbeat seeing the
        # lease cleared by that very completion must not cancel it.
        inference = asyncio.create_task(self.infer(job))
        lost_wait = asyncio.create_task(lost.wait())
        done, _ = await asyncio.wait({inference, lost_wait}, return_when=asyncio.FIRST_COMPLETED)
        lost_wait.cancel()
        if inference not in done:
            inference.cancel()  # closes the backend request so the model stops generating
            with suppress(asyncio.CancelledError):
                await inference
            raise LeaseLost(f"Lease for {job['id']} lost; inference aborted")
        await self.report(client, job, *inference.result())

    async def run(self, once: bool = False):
        async with httpx.AsyncClient(timeout=30, trust_env=False, transport=self.transport) as client:
            while True:
                try:
                    if time.monotonic() - self.last_heartbeat > 45:
                        await self.heartbeat(client)
                        self.last_heartbeat = time.monotonic()
                        self.write_status(connected=True, last_heartbeat=_now())
                        await self.fetch_self_status(client)
                    reply = await client.post(f"{self.coordinator}/api/worker/claim", headers=self.headers())
                    reply.raise_for_status()
                    self.write_status(connected=True)
                    job = reply.json().get("job")
                    if job:
                        print(f"Claimed {job['id']} ({job['model']}, {job['privacy']})")
                        self.write_status(job_status="running", job_model=job["model"], job_id=job["id"])
                        stop, lost = asyncio.Event(), asyncio.Event()
                        pulse = asyncio.create_task(self.maintain_lease(client, job, stop, lost))
                        try:
                            await self.run_until_lease_lost(client, job, lost)
                            print("Completed", job["id"])
                            completed = int(self.read_status().get("jobs_completed", 0)) + 1
                            self.write_status(job_status="idle", last_success=_now(),
                                              last_job_id=job["id"], last_error=None,
                                              jobs_completed=completed)
                        except LeaseLost as exc:
                            # The lease is gone, so a failure report would be rejected anyway.
                            print(f"Aborted: {exc}", file=sys.stderr)
                            self.write_status(job_status="idle", last_error=str(exc)[:300], last_error_at=_now())
                            if once:
                                raise
                        except Exception as exc:
                            print(f"Inference failed: {exc}", file=sys.stderr)
                            self.write_status(job_status="idle", last_error=str(exc)[:300], last_error_at=_now())
                            try:
                                failure = await client.post(f"{self.coordinator}/api/worker/complete", json={
                                    "job_id": job["id"], "lease_token": job["lease_token"],
                                    "ok": False, "error": str(exc)[:800]}, headers=self.headers())
                                failure.raise_for_status()
                            except Exception as report_exc:
                                print(f"Could not report failure: {report_exc}", file=sys.stderr)
                            if once:
                                raise
                        finally:
                            stop.set()
                            await pulse
                    elif once:
                        print("No matching jobs")
                        self.write_status(job_status="idle")
                except Exception as exc:
                    print(f"Coordinator unavailable or rejected request: {exc}", file=sys.stderr)
                    self.write_status(connected=False, last_error=str(exc)[:300], last_error_at=_now())
                    if once:
                        raise
                if once:
                    break
                await asyncio.sleep(self.poll_seconds)


def main():
    parser = argparse.ArgumentParser(description="Community AI Grid private-network worker")
    parser.add_argument("--once", action="store_true", help="Process one available job and exit")
    args = parser.parse_args()
    coordinator = env("CAG_COORD_URL", "http://127.0.0.1:8765")
    worker_token = env("CAG_WORKER_TOKEN")
    model = env("CAG_MODEL", "demo-model")
    backend = env("CAG_LLM_BACKEND", "openai")
    default_url = ("http://127.0.0.1:11434/api/chat" if backend == "ollama"
                   else "http://127.0.0.1:8080/v1/chat/completions")
    llm_url = env("CAG_LLM_URL", default_url)
    if not worker_token or not worker_token.startswith("cag_"):
        parser.error("Set CAG_WORKER_TOKEN to a token issued by the coordinator administrator")
    if coordinator.startswith("http://") and not coordinator.startswith(("http://127.0.0.1:", "http://localhost:")):
        parser.error("Refusing plaintext remote coordinator; use a local SSH tunnel or HTTPS")
    if not llm_url.startswith(("http://127.0.0.1:", "http://localhost:")):
        print("CAUTION: Your LLM_URL is not on localhost. Ensure this is a trusted endpoint.", file=sys.stderr)
    try:
        limits = {}
        if backend == "ollama":
            limits = {"context_tokens": int(env("CAG_OLLAMA_NUM_CTX", "1024")),
                      "cpu_threads": int(env("CAG_OLLAMA_NUM_THREAD", "2")),
                      "max_output_tokens": int(env("CAG_OLLAMA_MAX_OUTPUT_TOKENS", "32")),
                      "gpu_layers": int(env("CAG_OLLAMA_NUM_GPU", "0"))}
            limits.update(read_limits_override(env("CAG_LIMITS_PATH")))
        heartbeat_seconds = float(env("CAG_LEASE_HEARTBEAT_SECONDS", "30"))
        if not 1 <= heartbeat_seconds <= 300:
            raise ValueError("CAG_LEASE_HEARTBEAT_SECONDS must be between 1 and 300")
        configured = Worker(coordinator, worker_token, model, llm_url,
                            env("CAG_LLM_API_KEY"), float(env("CAG_WORKER_POLL_SECONDS", "3")),
                            backend=backend, lease_heartbeat_seconds=heartbeat_seconds,
                            status_path=env("CAG_STATUS_PATH"), **limits)
    except ValueError as exc:
        parser.error(str(exc))
    asyncio.run(configured.run(args.once))


if __name__ == "__main__":
    main()
