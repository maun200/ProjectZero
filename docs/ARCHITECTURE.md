# Architecture & ADRs

## Principles

1. Make a small vertical slice work before solving distributed training.
2. Coordinator and worker communicate via pull-only API; never remotely execute arbitrary scripts or shell commands.
3. Distinguish *model sharding*, *job parallelism*, and *privacy-preserving computation*. They are separate concerns.
4. API requests explicitly select `trusted` or `community`. Never silently downgrade privacy requirements.
5. Arbitrary worker output is **not proof of computation**. Validate before making earnings spendable.
6. Start with single coordinator process and SQLite, migrate to Postgres + queue only after observing bottlenecks.
7. Device support is capability-based (CPU/GPU/RAM/model compatibility), not a claim that every connected device can perform LLM inference.

## Interfaces v0.1

- `POST /api/jobs` (`CAG_USER_KEY` or a worker's separate `user_token`): queue messages, reserve 5 demo credits.
- `GET /api/jobs/{id}` (same user token): job status and result.
- `POST /api/jobs/{id}/cancel` (same user token): cancel a queued or running job; refund exactly once.
- `GET /api/balance` (user): balance in demo credits.
- `POST /api/admin/workers` (admin): enroll worker, issue worker and user token **once**.
- `GET /api/admin/workers` (admin): coarse worker health and provisional credits.
- `POST /api/admin/workers/{id}/settle` (admin): manually approve earned credits; not automatic verification.
- `POST /api/admin/workers/{id}/revoke` (admin): disable the worker token at once; its running jobs are requeued, or failed and refunded on the last attempt.
- `POST /api/admin/workers/{id}/rotate` (admin): issue a new worker token once; leases in progress stay valid.
- `POST /api/worker/heartbeat` (worker): device metadata and renew a leased job.
- `POST /api/worker/claim` (worker): fetch one compatible job; no job is pushed unsolicited.
- `POST /api/worker/complete` (worker): report success/failure, only with a valid unexpired lease.
- `GET /health` (unauthenticated): service liveness.

## State machine

`queued -> running -> completed`

`running -> queued` on reported failure when attempts remain, or once the lease has expired.

`running -> failed` if maximum retries exhausted; initial user's reserved credits are returned.

`queued|running -> cancelled` on user request; the reservation is refunded and the cleared lease makes a late heartbeat or completion fail with 409.

`queued -> failed` after `CAG_QUEUE_TIMEOUT_SECONDS` without a matching worker (refund).

`running -> queued|failed` when the operator revokes the assigned worker.

Lease expiry is evaluated inside a write transaction on every worker claim **and** every job-status request, so a job and its credit reservation cannot stay stuck in `running` while no worker is polling.

Claim, completion and heartbeat sample the lease-validation time **after** acquiring the SQLite writer lock. Heartbeat metadata and lease renewal commit together. A lease is expired at its deadline (`lease_until <= now`); waiting for a writer must not revive an expired lease. A background sweeper (`CAG_SWEEP_SECONDS`, default 30 s) applies lease expiry and the queue timeout in the same transactional `sweep()` used by the request paths, so cleanup no longer depends on polling. With `CAG_SWEEP_SECONDS=0` expiry is request-driven again.

Worker credits are issued only as **provisional** after a report of completion. Manual administrative approval may transfer a portion to the associated spendable user account. Idempotent lease validation prevents replaying a completion request.

## Threat model (small prototype)

Actors: administrator/coordinator; browser API user; manually enrolled worker; model server on the worker machine. Worker and coordinator are both trusted **only for lab workloads**. Public peer admission is not available.

Trust label on a worker means manually approved operator/device, not secure enclave attestation. Community-mode jobs are disclosed in plaintext to whichever permitted worker claims them.

## Big model mode — separate lab experiment

Try partitioned inference in a *separate test network* after measuring network bandwidth, memory, and latency. Candidate stacks: Petals, exo, or experimental llama.cpp RPC **only in a trusted isolated network**. Do not turn on internet-facing llama.cpp RPC. Sharded inference does not imply confidentiality. Maintain time-to-first-token, tokens/second, p50/p95 latency, bytes transferred, node dropout metrics; use small models and model-license compliance.

## Future: privacy-focused computing

Research multiparty computation with honest-majority/malicious models; quantify GPU speed and WAN traffic. Token splitting is not privacy. A design cannot make ordinary public workers private simply by sending them different token fragments.

## Future: heterogenous workers

| Device class | Potential use | Realism |
|---|---|---|
| GPU PC | Local whole-model inference | High |
| CPU laptop | Small quantized models | Moderate |
| ARM device / smartphone | Small model with battery/thermal limits | Experimental |
| Raspberry Pi | Low-throughput small tasks | Experimental |
| Smart plug / bulb | Usually no custom runtime, unsupported | Not viable for v0.1 |

## Out of scope v0.1

No automatic federated learning, no distributed model weights, no token-by-token privacy protocol, no internet-facing gateway, no blockchain, no fully verified marketplace.
