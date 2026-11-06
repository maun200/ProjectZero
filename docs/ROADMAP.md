# Milestones / definition of done

## Mission

ProjectZero should grow from single servers for households and clubs into a **global,
community-owned AI network: a real alternative to commercial AI providers**, run on hardware
people already own, independent of any single company or server. It will not match a data
center on the speed of one answer; it aims to win on cost, openness and independence.
Background and design options: [RESEARCH_GLOBAL_GRID.md](RESEARCH_GLOBAL_GRID.md).

## Phase 3 - Global community grid (long-term goal)

Nothing here is implemented yet. Order matters: a server must be safe to offer to strangers
before servers can be connected to each other.

- [ ] **Internet-ready single server.** Reverse-proxy and TLS guide, trusted-proxy
  `X-Forwarded-For` handling (#20), abuse reporting, retention and deletion rules, a privacy
  notice template for operators (see "Before you go public" in [SECURITY.md](SECURITY.md)).
- [ ] **Answers you can trust between strangers.** Result spot checks with trusted machines
  (#26) and reputation for machines, so credits keep their meaning in an open network.
- [ ] **Federation protocol (design first).** Server identity keys, exchange of load
  summaries, forwarding of work with a hop limit, the privacy level travels with the job, the
  origin server owns the job.
- [ ] **Credits across servers.** Per-server ledgers, signed receipts, periodic settlement
  between servers; a cheating server loses trust and is disconnected.
- [ ] **Finding a community.** A public, opt-in directory of servers (region, languages,
  models, registration mode) and joining from the app.
- [ ] **Regional routing.** Prefer nearby servers and machines to keep waiting times low.
- [ ] **Community-run public servers.** Only after the security items above are done.

## v0.3 - ProjectZero public release (done)

- [x] Self-registration (`off` / `invite` / `open`), member accounts, user-token rotation
- [x] Members register their own machines (community trust), per-member machine limit
- [x] Lobby, group and direct chat; online list of machines and people
- [x] Optional auto-settled credits; admin block and message moderation
- [x] Single-use enrollment codes and per-source rate limits
- [x] Local desktop app (English/German, light/anthracite) with AI chat, credits panel and compute sliders
- [x] One-line installers for Linux, macOS and Windows, tested in CI on all three
- [x] Privacy scanner for the public tree

## M0 — reproducible private prototype (current repo)

- [x] Source archive + reproducible virtual environment and container recipe
- [x] Coordinator, mock inference, polling UI, worker enrollment
- [x] Queueing, lease/heartbeat, limited retries, result polling
- [x] Non-transferable provisional compute credits + manual settlement
- [x] Distinct worker/user tokens and trust/consent scheduling
- [x] Unit/integration tests for auth, end-to-end, replay/refund, privacy eligibility
- [x] Deploy to a Proxmox container and run real multi-host jobs
- [ ] Multi-host benchmark (throughput, p50/p95 latency)
- [ ] Connect local llama.cpp model with appropriate quantization and license
- [ ] Verify behavior on Ubuntu, Windows and one older laptop
  - [x] Second physical device: Fedora laptop (i5 10th gen, CPU) as worker over a tunnel-only SSH key, incl. tunnel loss tests (2026-11-03, docs/ADD_DEVICE.md)
- [ ] Real measurements p50/p95 latency, throughput, GPU utilization, power consumption
  - [x] Small-sample CPU/GPU single-device benchmarks for qwen2.5-coder 1.5B/7B (docs/BENCHMARK_2026-11-03.md); no throughput, multi-worker or power measurements yet

## M1 — reliable community inference service

- [x] Per-process rate limiting; worker-token rotation & revoke (v0.2)
- [ ] TLS, user accounts, service tokens, user-token rotation, shared rate limiting
- [ ] Postgres migration, distributed queue, persistent jobs and metrics
- [x] Job cancellation, queue timeout, background lease sweeper, worker aborts on lease loss (v0.2)
- [ ] Asynchronous LLM streaming, back-pressure and fair allocation
- [ ] Structured model registry (RAM/VRAM, context, backend, license)
- [ ] Device-specific limits, thermal / energy constraints, dynamic model adaptation
- [ ] Real result validation; no automated release of earnings without verification
- [ ] Worker signing, sandboxing, untrusted participant threat model
- [ ] Public-ready privacy, security and GDPR assessment

## M2 — multiple machines serving a single big model

- [ ] In a separate isolated test LAN, measure segmented inference (Petals/exo/other)
- [ ] Compare WAN vs LAN: layer partitioning, throughput, TTFT and p95 latency
- [ ] Implement cluster planner with bandwidth/latency/capacity placement
- [ ] Failover when a shard disappears; KV cache lifetime policy
- [ ] Do not equate sharding with confidentiality

## M3 — compatible edge/mobile workers

- [ ] Android foreground-only client with opt-in charging/WLAN/thermal rules
- [ ] Raspberry Pi/ARM small-model or non-LLM compute adapter
- [ ] Capability schema + signed cross-platform releases
- [ ] Avoid smart-home devices whose firmware cannot safely run third-party code

## M4 — distributed fine-tuning and privacy R&D

- [ ] Adapter / LoRA training study before full pretraining
- [ ] Benchmark secure MPC and/or isolated trusted hardware against WAN cost
- [ ] Prevent poisoning, verify provenance, validate model licensing
- [ ] Document that fully decentralized pretraining may be uneconomical
