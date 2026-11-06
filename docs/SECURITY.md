# Security and privacy

**ProjectZero is experimental software for home networks and small groups that trust their
server operator.** Read this page before you run a server that other people use.

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Use GitHub's private
vulnerability reporting instead: on the repository page, open the **Security** tab and choose
**Report a vulnerability**. Never paste tokens, prompts, personal data or server addresses
into issues, discussions or pull requests.

## What every user should know

- **Requests are not private.** Prompts, AI answers and chat messages are stored unencrypted in
  the server's SQLite database. The operator can read them, and the machine that computes an AI
  request reads the full prompt. Do not send personal, confidential or secret data.
- **Answers are not verified.** A machine can return a wrong or made-up answer. Nothing checks
  that work was really done.
- **Credits are play money.** They decide who may use how much compute on one server. They have
  no monetary value and cannot be transferred.
- **Your access key is your login.** Anyone with it can spend your credits and read your chats.
  The app keeps it in a file only your user account can read and never shows it unless you ask.

## Deployment modes

| Mode | How | Who can reach the server | Suitable for |
| --- | --- | --- | --- |
| Local | `projectzero server` / `projectzero demo`, or `CAG_BIND_IP=127.0.0.1` | this computer only | trying it out, development |
| Private network | `projectzero server --lan`, or `CAG_BIND_IP` set to a LAN/VPN address | every device on that network or VPN | a household, a club LAN, a VPN such as WireGuard or Tailscale |
| Tunnel-only workers | `scripts/grid_device.py` + `scripts/grid_join.py` (see [ADD_DEVICE.md](ADD_DEVICE.md)) | devices with a restricted SSH key that can only forward the coordinator port | trusted machines outside the LAN |
| Internet | an HTTPS reverse proxy in front of a private-network server | everyone | **not recommended yet**; see "Before you go public" |

Plain `http://` is only acceptable on loopback, inside a private network or through an
encrypted tunnel. The client app refuses plain `http://` to public addresses.

## Protections that exist today

- **Separate credentials.** Admin key (operator), user/member tokens (spend credits, chat) and
  machine tokens (claim and complete jobs). Tokens are stored as SHA-256 hashes.
- **Pull-only workers.** The server never connects to a worker and never sends code. Workers
  only pass the request text to a local model the owner chose; Ollama URLs must be loopback.
- **Trust levels.** Jobs marked `trusted` only go to machines the operator enrolled as trusted.
  Everything registered through the app is `community`, and community jobs require explicit
  consent in the request.
- **Registration control.** `CAG_REGISTRATION=off` (default), `invite` (single-use or limited
  invite codes) or `open`. Member self-service is limited to self-registered accounts; the
  shared demo key cannot add machines or chat.
- **Rate limits.** Per authenticated identity, per source address for registration and for
  headless enrollment, and a global registration cap. Request bodies over 1 MiB are rejected
  before parsing.
- **Revocation.** Admins can revoke or rotate machine tokens, block a member (their login stops
  working and their machines are revoked) and delete chat messages. Members can rotate their own
  token and remove their own machines.
- **Leases.** Jobs held by a vanished or revoked machine go back to the queue or are refunded.
- **Local app hardening.** The desktop app listens on 127.0.0.1 only, checks the Host header
  (DNS rebinding) and requires a per-start session secret on every request, so other websites in
  the same browser cannot drive it. It forwards only an allowlist of server API paths.
- **Container.** The Docker image runs as a non-root user with a read-only root filesystem, no
  capabilities, `no-new-privileges`, and CPU, memory and process limits.

## Before you go public

These are known gaps. Do not offer a server to strangers on the internet until you have
addressed the ones that matter for you:

1. TLS termination and a reviewed reverse proxy; the coordinator itself speaks plain HTTP.
2. Rate limiting behind a proxy needs the real client address (`X-Forwarded-For` handling is not
   implemented; behind a proxy all users share one bucket).
3. Abuse handling beyond blocking: reporting, moderation tools, retention and deletion policies,
   terms of use and a privacy notice (GDPR if you serve people in the EU).
4. Result verification and protection against fake or colluding machines (Sybil attacks).
5. Encrypted storage or minimization of prompts and chats; there is no end-to-end encryption.
6. A shared database and rate limiter for more than one coordinator process (SQLite and the
   in-memory limiter are single-process).
7. Signed releases, dependency hashes and an SBOM.
8. Prompt-injection and malicious-output handling for anyone who acts on AI answers.
9. Private inference (trusted execution or cryptographic methods). Splitting a model across
   machines does **not** hide data.

## Operator checklist

- Generate independent random keys (`python -c "import secrets; print(secrets.token_urlsafe(32))"`);
  never use the placeholders. `projectzero server` does this for you.
- Keep `.env`, `server.env`, the database volume and backups readable only by the operator.
- Allow only models whose license you accept, via `CAG_ALLOWED_MODELS` / `--models`.
- Run the server in a dedicated VM or container with resource limits, not on a machine that
  hosts other important services.
- Keep `CAG_AUTO_SETTLE=false` unless your group trusts every member's machine.
