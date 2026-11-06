"""Local administrator helper: issue a single-use, time-limited self-enrollment code for
/api/enroll.

The coordinator tracks redemption server-side (`enroll_codes` table): the first device to
redeem the code consumes it, so still share it with only the one device you intend to enroll,
but a second attempt (yours or anyone else's) is rejected rather than silently succeeding.
"""
import argparse
import json
import os
import sys

import httpx


def main():
    p = argparse.ArgumentParser(description="Issue a self-enrollment code for a community worker")
    p.add_argument("--ttl-seconds", type=int, default=3600)
    args = p.parse_args()
    key = os.getenv("CAG_ADMIN_KEY", "")
    if not key:
        sys.exit("Set CAG_ADMIN_KEY in the environment first")
    base = os.getenv("CAG_COORD_URL", "http://127.0.0.1:8765").rstrip("/")
    response = httpx.post(f"{base}/api/admin/enroll-codes",
                          headers={"Authorization": f"Bearer {key}"},
                          json={"ttl_seconds": args.ttl_seconds}, timeout=15)
    response.raise_for_status()
    print(json.dumps(response.json(), indent=2))
    print("IMPORTANT: Share this code only with the one device you intend to enroll.", file=sys.stderr)

if __name__ == "__main__":
    main()
