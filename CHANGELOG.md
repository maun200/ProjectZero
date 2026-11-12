# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- Desktop app: changing "Model on this device" snapped back to the old value and never actually
  saved. `fillMachineModels()` reset the `<select>` to the server's last-known model on every
  call, including the one the model-change handler itself made before saving the new value —
  so the new selection was always overwritten before it reached the server. The reset now only
  happens when the option list itself is rebuilt, not on every refresh.

### Added

- `grid_device.py update <name> --model <tag>` and a matching coordinator endpoint
  (`POST /api/admin/workers/{id}/model`) to move a device to a different operator-approved
  model in place — no new worker token, no new bundle to carry to the device, trust level and
  credit history untouched. Previously the only way to change a device's model was
  `remove` + `add`, which reissued its token and required transferring a fresh bundle.

### Documentation

- Long-term goal stated in the README and roadmap: a global, federated, community-owned AI
  network as an alternative to commercial providers (Phase 3, not implemented yet).

## [0.3.0] - 2026-11-06

First public release as ProjectZero.

### Added

- **Desktop app** (`projectzero`): a local web app on 127.0.0.1 with AI chat that spends
  credits, a credits panel, sliders for CPU threads, GPU layers, answer length and context,
  lobby/group/direct chat, an online list of machines and people, English and German, light and
  anthracite themes, and a phone-sized layout. Markdown in AI answers is rendered safely.
- **One-line installers** for Linux/macOS (`install.sh`) and Windows (`install.ps1`), without
  administrator rights; tested in CI on all three systems with a full end-to-end smoke test.
- `projectzero server` to self-host a coordinator with generated keys, and `projectzero demo`
  to try everything locally with a mock model.
- Self-registration of members (`CAG_REGISTRATION=off|invite|open`) with invite codes, signup
  credits and per-source rate limits.
- Member accounts: display name, `/api/me`, token rotation, own machines (`community` trust,
  limited by `CAG_MAX_MACHINES_PER_MEMBER`).
- Lobby, group and direct chat channels (plaintext on the server, not a private messenger).
- Optional `CAG_AUTO_SETTLE`: credit finished jobs to the machine owner at once (unverified).
- Admin moderation: delete messages, block members (login and machines revoked).
- Single-use enrollment codes and per-source limits for headless devices (`/api/enroll`).
- Worker self-status endpoint (`GET /api/worker/me`).
- Built-in `mock` worker backend for demos and tests.
- Privacy and secret scanner for the public tree (`scripts/check_public_tree.py`), blocking in CI.
- MIT license, contributing guide, code of conduct, issue and PR templates, Dependabot.

### Changed

- Project renamed from Community AI Grid to ProjectZero; package `projectzero`, version 0.3.0.
- Member self-service is limited to self-registered accounts; the shared demo key can only
  submit jobs.
- `compose.yaml` forwards every coordinator setting (a test now enforces this).
- Documentation rewritten in English with a German README.

### Security

- Blocking a member also revokes a worker paired 1:1 with that account.
- The local app checks the Host header and requires a per-start session secret on every request.

## [0.2.0]

### Added

- Worker token revocation and rotation.
- Rate limiting per authenticated identity and coordinator process.
- Job cancellation by users with refund.
- Queue timeout with automatic refund and a background sweeper for expired leases.
- Operator model allowlist (`CAG_ALLOWED_MODELS`) for jobs and worker enrollment.
- Automated tests and CI.

## [0.1.0]

### Added

- First prototype: FastAPI + SQLite coordinator, persistent jobs, provisional credit ledger.
- Pull-based workers that call a local OpenAI-compatible model endpoint and never run downloaded code.
- Per-worker credentials, model matching, trust-aware scheduling (`trusted` and `community`),
  leases, heartbeats, retries and refunds.
- Browser test UI and OpenAPI docs; mock backend for orchestration tests without AI.
