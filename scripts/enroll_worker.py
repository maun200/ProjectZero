"""Local administrator helper. Reads credentials only from environment."""
import argparse
import json
import os
import sys

import httpx


def main():
    p = argparse.ArgumentParser(description="Provision a worker token once (keep its output secret)")
    p.add_argument("--name", required=True)
    p.add_argument("--model", default="demo-model")
    p.add_argument("--trust", choices=["trusted", "community"], default="trusted")
    args = p.parse_args()
    key = os.getenv("CAG_ADMIN_KEY", "")
    if not key:
        sys.exit("Set CAG_ADMIN_KEY in the environment first")
    base = os.getenv("CAG_COORD_URL", "http://127.0.0.1:8765").rstrip("/")
    response = httpx.post(f"{base}/api/admin/workers",
                          headers={"Authorization": f"Bearer {key}"},
                          json={"name": args.name, "model": args.model, "trust_level": args.trust}, timeout=15)
    response.raise_for_status()
    print(json.dumps(response.json(), indent=2))
    print("IMPORTANT: Save worker_token securely. This output is sensitive.", file=sys.stderr)

if __name__ == "__main__":
    main()
