"""Local administrator helper: list, revoke or rotate workers. Reads credentials only from environment."""
import argparse
import json
import os
import sys

import httpx


def run(command: str, worker_id: str | None, client: httpx.Client, base: str, admin_key: str) -> dict | list:
    if command not in ["list", "revoke", "rotate"]:
        raise ValueError(f"Invalid command: {command}")
    if command in ["revoke", "rotate"] and worker_id is None:
        raise ValueError("Worker ID is required for revoke and rotate commands")
    headers = {"Authorization": f"Bearer {admin_key}"}
    if command == "list":
        response = client.get(f"{base}/api/admin/workers", headers=headers)
    elif command == "revoke":
        response = client.post(f"{base}/api/admin/workers/{worker_id}/revoke", headers=headers)
    elif command == "rotate":
        response = client.post(f"{base}/api/admin/workers/{worker_id}/rotate", headers=headers)
    response.raise_for_status()
    return response.json()


def main():
    p = argparse.ArgumentParser(description="Manage worker tokens and credentials")
    p.add_argument("command", choices=["list", "revoke", "rotate"])
    p.add_argument("--worker-id", required=False)
    args = p.parse_args()
    key = os.getenv("CAG_ADMIN_KEY", "")
    if not key:
        sys.exit("Set CAG_ADMIN_KEY in the environment first")
    base = os.getenv("CAG_COORD_URL", "http://127.0.0.1:8765").rstrip("/")
    with httpx.Client(timeout=15, trust_env=False) as client:
        result = run(args.command, args.worker_id, client, base, key)
    print(json.dumps(result, indent=2))
    if args.command == "rotate":
        print("IMPORTANT: Save the new worker_token securely. This output is sensitive.", file=sys.stderr)


if __name__ == "__main__":
    main()
