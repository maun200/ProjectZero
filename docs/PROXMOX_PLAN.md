# Proxmox deployment checklist (do not deploy automatically)

**Target:** patched dedicated Ubuntu/Debian VM or restricted LXC, never the Proxmox management host. The VM can run the coordinator Docker compose with resource caps. Worker models run on desktop/laptops; the coordinator has no GPU requirement.

**Approval gate:** this is a proposal only. Obtain operator approval for the exact new test VMID, storage, bridge and resource allocation before creating or changing anything. Prefer a new dedicated VM; an existing production VM/LXC is not a target. Proposed starting budget: 1–2 vCPU, 2 GiB RAM, 16 GiB disk, subject to actual free capacity. No Proxmox access or deployment has been tested by the local review.

1. Take a configuration snapshot/backup of the target VM and note rollback instructions. Check CPU/memory/disk/network headroom on the Proxmox host and avoid conflicting ports.
2. Install Docker/Compose inside the isolated VM, clone the **private** GitHub repository with read-only credentials.
3. Copy `.env.example` to `.env`, generate two independent 32+ byte tokens locally, put them in `.env` with `chmod 600 .env`; keep `CAG_BIND_IP=127.0.0.1`.
4. `docker compose config` and review ports, volumes, `read_only`, `no-new-privileges`, CPU/memory limits. Start only after approval.
5. `docker compose up --build -d`, `docker compose ps`, `curl http://127.0.0.1:8765/health` and `docker compose logs --tail=50` with secrets redacted.
6. Enable SSH key-based access to VM; create client-side tunnels to coordinator loopback. No inbound public firewall rule, no public DNS or reverse proxy.
7. Enroll each worker on coordinator with separate tokens, save `worker_token` and `user_token` securely. Start local mock first, then separately approve a small real LLM model.
8. Validate queue -> worker -> completion, first failure/refund, worker disconnect/retry, trusted-vs-community non-downgrade, balance and settlement.
9. Establish volume backup policy: SQLite plaintext includes prompts; retention and encryption require a policy before real users.
10. Stop safely with `docker compose down` (preserves volume). **Never** use `down -v` unless intentionally deleting the database.

## Rollback proposal (requires operator approval)

- Before installing in the approved new VM, create a snapshot if supported: `qm snapshot <NEW_TEST_VMID> pre-cag`. Confirm the ID belongs to that new test VM.
- To stop only the coordinator, run `docker compose down` inside the VM's project directory. Retain the volume and protected backups.
- If VM rollback is needed, confirm that losing changes since the snapshot is acceptable; then stop that test VM and use `qm rollback <NEW_TEST_VMID> pre-cag`. Do not roll back or delete existing workloads. VM deletion is not part of this runbook.
- Validate the mock cycle before considering any real model. Keep the inference backend on each worker's loopback interface, with a separately approved model and explicit resource limits. Do not download models or enable GPU passthrough as part of coordinator deployment.

Security warning: SSH forwarding to a host is not proof of worker integrity. Local models can still receive full user prompts. MPC is not implemented.
