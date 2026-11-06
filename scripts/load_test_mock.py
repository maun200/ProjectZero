"""Multi-process mock load test on loopback: N workers, M jobs, optionally one worker crash.

Checks exactly-once settlement under real processes. Mock orchestration only;
the timings are not a performance benchmark.

    python scripts/load_test_mock.py --workers 3 --jobs 9 --kill-one
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx

from smoke_test import free_port, wait_for

ROOT = Path(__file__).resolve().parent.parent
TERMINAL = {"completed", "failed", "cancelled"}


def bounded(low: int, high: int):
    def parse(text: str) -> int:
        value = int(text)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return value
    return parse


def read_db(path: Path, sql: str, params=()):
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        return db.execute(sql, params).fetchall()


def fail(reason: str):
    print("FAIL: " + reason)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workers", type=bounded(1, 8), default=3)
    parser.add_argument("--jobs", type=bounded(1, 100), default=12)
    parser.add_argument("--kill-one", action="store_true", help="SIGKILL the worker of the first running job")
    parser.add_argument("--timeout", type=bounded(10, 600), default=120)
    args = parser.parse_args()
    if args.kill_one and args.workers < 2:
        parser.error("--kill-one needs at least two workers")

    coord, mock = free_port(), free_port()
    admin, user = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    scratch = ROOT / ".pytest_cache" / "load-test"
    scratch.mkdir(parents=True, exist_ok=True)
    # fuseblk may keep .fuse_hidden files briefly; leftovers stay in the ignored cache.
    with tempfile.TemporaryDirectory(dir=scratch, ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "load.sqlite3"
        env = {k: v for k, v in os.environ.items() if not k.startswith("CAG_")}
        env.update(CAG_ADMIN_KEY=admin, CAG_USER_KEY=user, CAG_DB_PATH=str(db_path),
                   CAG_INITIAL_DEMO_CREDITS="1000", CAG_LEASE_SECONDS="10", CAG_SWEEP_SECONDS="1",
                   CAG_QUEUE_TIMEOUT_SECONDS="600", CAG_RATE_LIMIT_PER_MINUTE="600",
                   CAG_ALLOWED_MODELS="demo-model", CAG_MOCK_DELAY_SECONDS="1")
        servers, workers = [], {}
        try:
            for appname, port in [("coordinator.app.main:app", coord), ("worker.mock_llm:app", mock)]:
                servers.append(subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", appname, "--host", "127.0.0.1", "--port", str(port),
                     "--no-access-log"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            base = f"http://127.0.0.1:{coord}"
            wait_for(base + "/health", servers[0])
            wait_for(f"http://127.0.0.1:{mock}/openapi.json", servers[1])
            admin_headers = {"Authorization": "Bearer " + admin}
            user_headers = {"Authorization": "Bearer " + user}
            with httpx.Client(timeout=10, trust_env=False) as client:
                for i in range(args.workers):
                    enrolled = client.post(base + "/api/admin/workers", headers=admin_headers,
                                           json={"name": f"load-{i}", "model": "demo-model"})
                    enrolled.raise_for_status()
                    worker_env = dict(env, CAG_COORD_URL=base, CAG_WORKER_TOKEN=enrolled.json()["worker_token"],
                                      CAG_MODEL="demo-model", CAG_LLM_BACKEND="openai",
                                      CAG_LLM_URL=f"http://127.0.0.1:{mock}/v1/chat/completions",
                                      CAG_WORKER_POLL_SECONDS="1", CAG_LEASE_HEARTBEAT_SECONDS="2")
                    workers[enrolled.json()["id"]] = subprocess.Popen(
                        [sys.executable, "-m", "worker.client"], cwd=ROOT, env=worker_env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                started = time.monotonic()
                job_ids = []
                for i in range(args.jobs):
                    submitted = client.post(base + "/api/jobs", headers=user_headers, json={
                        "model": "demo-model", "messages": [{"role": "user", "content": f"synthetic load {i}"}]})
                    submitted.raise_for_status()
                    job_ids.append(submitted.json()["id"])

                killed_job = killed_worker = None
                deadline = started + args.timeout
                while args.kill_one and killed_job is None:
                    running = read_db(db_path, "SELECT id, assigned_worker FROM jobs WHERE status='running' LIMIT 1")
                    if running:
                        killed_job, killed_worker = running[0]["id"], running[0]["assigned_worker"]
                        workers[killed_worker].send_signal(signal.SIGKILL)
                    elif time.monotonic() > deadline:
                        fail("no job ever became running")
                    else:
                        time.sleep(0.05)

                # Poll the database read-only: API polling would hit the user's own rate limit.
                while True:
                    rows = read_db(db_path, "SELECT id, status, attempts, assigned_worker FROM jobs")
                    if len(rows) == args.jobs and all(r["status"] in TERMINAL for r in rows):
                        break
                    if time.monotonic() > deadline:
                        fail(f"timeout; states {sorted(r['status'] for r in rows)}")
                    time.sleep(0.5)
                wall = time.monotonic() - started

                states = {r["id"]: r for r in rows}
                if any(r["status"] != "completed" for r in rows):
                    fail(f"not all jobs completed: {sorted(r['status'] for r in rows)}")
                balance = client.get(base + "/api/balance", headers=user_headers)
                balance.raise_for_status()
                if balance.json()["demo_credits"] != 1000 - 5 * args.jobs:
                    fail(f"user balance {balance.json()['demo_credits']} != {1000 - 5 * args.jobs}")
                listed = client.get(base + "/api/admin/workers", headers=admin_headers)
                listed.raise_for_status()
                if sum(w["provisional_credits"] for w in listed.json()) != 5 * args.jobs:
                    fail("provisional credits do not match completed jobs")
                rewards = read_db(db_path, "SELECT job_id, COUNT(*) AS n FROM ledger "
                                           "WHERE note='unverified_provisional_reward' GROUP BY job_id")
                if sorted(r["job_id"] for r in rewards) != sorted(job_ids) or any(r["n"] != 1 for r in rewards):
                    fail("each job must be rewarded exactly once")
                requeued = False
                if killed_job:
                    final = states[killed_job]
                    requeued = final["attempts"] >= 2 and final["assigned_worker"] != killed_worker
                    if not requeued:
                        fail("killed worker's job was not completed by another worker")
            print(json.dumps({"status": "PASS", "label": "mock orchestration test - not a performance benchmark",
                              "jobs": args.jobs, "workers": args.workers, "killed_job_requeued": requeued,
                              "wall_seconds": round(wall, 1)}))
        finally:
            for proc in [*workers.values(), *servers]:
                if proc.poll() is None:
                    proc.terminate()
            for proc in [*workers.values(), *servers]:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


if __name__ == "__main__":
    main()
