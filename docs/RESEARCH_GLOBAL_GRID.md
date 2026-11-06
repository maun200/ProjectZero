# Research and vision: a global grid of heterogeneous devices

> **Status: research and vision. Nothing in this document is implemented unless it is listed
> under "Exists today".**
>
> | Exists today | Does not exist |
> |---|---|
> | One coordinator (FastAPI + SQLite), pull-only workers | No model splitting across machines |
> | Each job runs a whole model on one machine | No federation between coordinators |
> | Trust levels (`trusted` / `community`) chosen per job | No mobile (Android/iOS) workers |
> | Leases, retries, refunds, credits (provisional, settled) | No private / confidential inference |

This document collects what was examined about turning the grid into a network of many,
mostly consumer, devices that together offer a useful alternative to commercial inference
providers. The two goals behind it:

1. **Price:** participants earn credits by contributing compute; the only real cost is the
   electricity of hardware they already own.
2. **Independence:** no single company or server is required to run the network.

The guiding observation of this document is simple: **many devices add throughput, not
single-request speed.** Every design below follows from where network latency does and does
not hurt.

---

## 1. Where latency hurts and where it does not

A language model generates one token at a time; token *n+1* depends on token *n*. That fixes
the cost structure of any distributed design:

| Split at | Network crossings | Effect of 50-150 ms wide-area latency |
|---|---|---|
| **Per token** (model layers on different machines) | one round through all shards for every generated token | dominates; adds directly to every token |
| **Per task step** (whole model per machine, task cut into steps) | one per step; a step takes seconds | small; text is cheap to send |
| **Per job** (today) | one claim and one report per job | negligible |

The 50-150 ms range is an **estimate** for consumer wide-area round trips between cities or
countries (typical published ISP latency figures); it was not measured for this project. A
measurement is listed under experiment E3.

Consequence: wide-area designs should split **tasks**, not **models**. Model splitting is
only reasonable between machines that are close to each other on the network (section 5).

---

## 2. Mobile workers

### Android

Feasible. Recent flagship phones have enough memory for the currently approved small model
(`qwen2.5-coder:1.5b`, roughly 1 GB as Q4 weights). The binding limit is heat and battery,
not peak speed: phones throttle under sustained load. No sustained-load measurement exists
yet (experiment E1).

Two stages:

1. **Spike without code changes.** Termux (from F-Droid) provides `python`, `openssh` and a
   `llama.cpp` build. `scripts/grid_join.py` then runs unchanged, with `llama-server` as an
   OpenAI-compatible backend (`CAG_LLM_BACKEND=openai`). Limits: Android kills background
   processes aggressively (the "phantom process killer" from Android 12), so the session needs
   a wake lock and must stay in the foreground. There are no charging or thermal rules.
2. **Native app.** A foreground service (the documented Android mechanism for long-running
   user-visible work), `llama.cpp` through JNI, and the three worker endpoints
   (`heartbeat`, `claim`, `complete`) reimplemented in Kotlin. Opt-in rules:
   - work only while charging (`BatteryManager`),
   - only on Wi-Fi (`ConnectivityManager`),
   - stop above a thermal status threshold (`PowerManager.getThermalStatus()`),
   - token stored in the Android Keystore.

   Transport: HTTPS to the coordinator rather than an embedded SSH client. The worker's
   existing refusal of plaintext remote coordinators stays in force.

Why the existing protocol suits phones: workers only pull, so no inbound port is needed and
carrier-grade NAT is not a problem. A phone that loses its network simply lets the lease
expire; the coordinator requeues or refunds the job. No coordinator change is required for
that.

### iOS

Hard. iOS does not allow long-running background computation. `BGProcessingTask` runs when
the system decides, typically while charging, and only for a limited time. The realistic mode
is **foreground only**: app open, screen on, device charging (for example overnight).
`llama.cpp` and MLX both run on Apple GPUs through Metal, so the technical side is fine.
Distribution requires a paid developer account (TestFlight), or re-signing every 7 days with
a free account. A jailbreak-based approach remains out of scope (see `docs/ROADMAP.md`,
backlog).

### Coordinator additions for mobile

- Heartbeat capability fields: on battery / charging, thermal state, network type.
- Per-device model limits, so a phone is never offered a job for a model it cannot hold.

---

## 3. Task decomposition for small models

This is the most promising direction, because it moves the network cost from "per token" to
"per step" (section 1).

Patterns that decompose well:

- **Map-reduce over long inputs:** summarise chunks in parallel, then merge.
- **Chunked translation, classification, extraction:** independent pieces, embarrassingly
  parallel.
- **Generate and check:** one model writes code, tests run, another model fixes failures.
  The tests double as verification.
- **Voting / ensembles:** several small models answer, an aggregator merges. Published work on
  "mixture of agents" (Wang et al., 2024) reports that ensembles of open models can approach
  larger models on some benchmarks; this has not been reproduced here.

Limits:

- Tasks that need deep reasoning over the whole context do not decompose well; large models
  stay clearly better there.
- **Planning the decomposition** needs a capable model. That is a job for the strongest
  nodes or a regional cluster (section 5).
- Errors compound across steps; checks between steps are needed.
- Decomposition adds tokens (planning, repeated context, merging), which eats part of the
  saving. The size of that overhead is unknown (experiment E2).

Interface between steps: **plain text**, not token IDs. Different model families use
different vocabularies, so token IDs are not portable. Text is the common format in a
network of heterogeneous models.

Side effect on privacy: each worker sees only a fragment. That reduces exposure per worker
but **is not protection**; fragments can contain sensitive data (section 8).

---

## 4. Federated coordinators

Goal: no single server. Model: federation in the style of e-mail or Matrix. Each node is a
full coordinator for its region; nodes exchange load summaries and forward work.

### Routing

- Every node keeps a live view of its own workers (load, measured speed), fed by the
  heartbeats that already exist.
- Nodes periodically exchange compact summaries, for example "40 free slots for 7B models,
  queue wait 3 s".
- A saturated node forwards task steps to a neighbour, with a **hop limit** so work cannot
  circulate.

### Rules that keep it correct

- **The origin node owns the job.** Other nodes execute steps; the result returns to the
  origin, which books the credits.
- **The privacy level travels with the job.** A `trusted` job is never forwarded to a node
  or worker that does not meet that level. Silent downgrades stay forbidden
  (`docs/ARCHITECTURE.md`, principle 4).
- **Each step is counted once.** The existing lease model is extended to leases issued by a
  remote node.
- **Every forward exposes the data to one more operator.** Users must be able to see this
  and opt out.

### Credits across nodes

The hard part is accounting, not routing.

| Option | Independence | Effort |
|---|---|---|
| One central ledger | low; single point of failure | low |
| **Per-node ledgers, signed receipts, periodic settlement between nodes** | high | medium |
| Blockchain | high | very high; out of scope (`docs/ROADMAP.md`) |

Recommended: per-node ledgers. A user has a home node. A remote node that computes for that
user issues a signed receipt; nodes settle against each other periodically. A node that
cheats loses trust and is disconnected.

### Direction of connections

The coordinator should **not** open connections to workers. That would require inbound ports
on home devices and phones, and would break the pull-only principle that is one of the
project's main security properties. Matching works with pull: workers report capabilities and
only receive jobs they can serve.

---

## 5. Regional low-latency clusters

Machines that are a few milliseconds apart (same city, same provider, same LAN) can share a
model that is too large for any one of them. This is pipeline (layer) splitting.

- Candidate stacks: `llama.cpp` RPC, exo, Petals (Borzunov et al., 2023, demonstrated
  layer-split inference over the internet with replicated shards).
- **Integration with the grid:** a cluster appears to the coordinator as **one worker**. A head
  machine runs `llama-server` with `--rpc` to the other machines and enrolls normally. No
  coordinator change is needed.
- `llama.cpp` RPC has no authentication and must only run inside an isolated, trusted network
  (already stated in `docs/ARCHITECTURE.md`).
- Transport between cluster members: a WireGuard-based mesh (self-hosted, for independence)
  or QUIC, rather than per-pair SSH tunnels. SSH key management does not scale to many pairs,
  TCP-over-TCP tunnels suffer head-of-line blocking, and two home devices behind NAT cannot
  reach each other over SSH without a relay.

Limits:

- The slowest member bounds the pipeline. A throttling device slows the whole cluster.
- A missing member stalls every request in flight unless shards are replicated, which costs
  extra machines.
- More members raise total throughput when many requests run concurrently; they do **not**
  make one request faster.
- Phones are poor cluster members: unstable networks, throttling, frequent disconnects.

---

## 6. Activation transfer formats for layer splitting

Between pipeline stages, what crosses the network is not tokens (2-4 bytes each) but the
**hidden state**: one vector of `hidden_size` numbers per token per hop.

Derived from published model configurations (arithmetic, not a measurement):

| Model | hidden_size | Bytes per token per hop, fp16 | int8 |
|---|---|---|---|
| Llama 3 8B | 4096 | 8 KiB | 4 KiB |
| Llama 3 70B | 8192 | 16 KiB | 8 KiB |

Options to make transfer cheaper:

- Quantise activations (fp16 to int8 or fp8); quality impact must be measured per model.
- Binary framing (for example Protobuf) over long-lived QUIC streams instead of JSON and one
  request per token.
- Batch several tokens or several requests per message.
- Keep the KV cache local to each stage; only the new vector moves.

None of this removes the real bottleneck. **Bandwidth is not the limit; round-trip latency
is.** A few KiB per token is small even on mobile networks, but each hop still costs its full
round trip.

---

## 7. Speculative decoding

Generating is sequential, but **verifying** several tokens can be done in one forward pass.
Speculative decoding (Leviathan et al., 2023; Chen et al., 2023) exploits this:

1. A small, fast **draft** model guesses the next k tokens.
2. The large **target** model checks all k in a single pass.
3. Tokens up to the first mismatch are accepted; the output distribution equals that of the
   target model alone.

The cited papers report speed-ups of roughly 2-3x on their setups. That figure depends on how
often the draft is right and has not been measured here (experiment E4).

Related approaches:

- **Jacobi / lookahead decoding** (Santilli et al., 2023; Fu et al., 2024): treat the next n
  tokens as unknowns, initialise with guesses, iterate in parallel until they stop changing.
- **Multi-token prediction heads** (Medusa, Cai et al., 2024; DeepSeek-V3 multi-token
  prediction).
- **Diffusion language models:** generate and refine all positions in parallel. Early stage.

Why it fits the grid: run the **draft model on a weak or local device** (phone, laptop) and
the **target model on a strong machine or regional cluster**. One round through the cluster
then covers k tokens instead of one, so per-hop latency is spread over several tokens. Weak
devices contribute to large-model speed instead of only running small models.

Constraint: draft and target must share a tokenizer, i.e. come from the same model family
(for example a 0.5B and a 32B model of the same series). `llama.cpp` supports a draft model
(`--model-draft`) and RPC splitting; combining the two is experiment E4.

---

## 8. Privacy

- Prompts are plaintext on the coordinator and on the worker that runs them. This is true
  today and stays true for every design in this document.
- **Token splitting alone does not hide data.** Sending different fragments or layers to
  different workers does not make ordinary workers private. Intermediate activations can leak
  prompt content, and the first pipeline stage sees the tokens directly.
- Task decomposition limits how much one worker sees; it is not confidentiality.
- Real options are limited: confidential-computing hardware (data-centre GPUs with trusted
  execution) or secure multi-party computation, which is far slower over wide-area networks
  (`docs/ARCHITECTURE.md`, "Future: privacy-focused computing").
- The honest product position is two tiers: **public tasks** that any community worker may
  see, and **confidential tasks** that only run on trusted machines. The `trusted` /
  `community` split already exists.

Two further open risks for an open network:

- **Verification:** a worker can return garbage and still claim credits. Options: redundant
  execution of a sample of steps at temperature 0 with a fixed seed, per-device reputation,
  credits released only after checks. Expected cost: extra compute proportional to the sampled
  share.
- **Sybil attacks:** once credits have value, one person can enroll many fake devices.
  Enrollment friction and slowly growing reputation are needed.

---

## 9. Economics: "only electricity"

Cost per token = power draw x electricity price / tokens per second.

Worked example with **assumed** values (not measured): 300 W, 0.35 EUR/kWh, 30 tokens/s,
one request at a time gives about 1 EUR per million tokens. Commercial prices for small open
models are often below that (public price lists; varies by provider and changes often).

What changes the picture:

- **Batching:** serving several requests at once on one GPU (`llama.cpp --parallel`, vLLM)
  multiplies throughput at similar power.
- **Hardware that is on anyway:** only the extra draw under load counts.
- **Keeping models loaded:** the current worker unloads after every job (`keep_alive=0`).
  Measured cold-load times on the desktop CPU were 3.22 s for the 1.5B model and 10.75 s for
  the 7B model (`docs/BENCHMARK_2026-11-03.md`). An operator-controlled keep-alive setting
  would avoid paying that per job.

For participants the main gain is independence: they pay with compute, not money.

---

## 10. Experiments we could run next

Each experiment has a measurable success criterion. None has been run.

| ID | Experiment | Measure | Success criterion |
|---|---|---|---|
| E1 | Android phone via Termux as worker, `qwen2.5-coder:1.5b` | tokens/s at minute 1 and minute 10 of sustained load; battery temperature; job success rate | 10-minute throughput at least 50 % of minute-1 throughput; no failed jobs; device stays below the OS thermal warning |
| E2 | Map-reduce summarisation of a long document with the 1.5B model on 3 workers vs. one 7B model on one machine | wall time; total tokens; blind quality rating on a fixed set of 20 documents | decomposed version not slower and rated at least equal on 14 of 20 |
| E3 | Wide-area latency survey between consenting participants | p50 / p95 round-trip time between pairs; jitter | data set of at least 20 pairs; defines which pairs qualify for a cluster |
| E4 | Speculative decoding: 0.5B draft locally, larger target split over 2 LAN machines with `llama.cpp` RPC | tokens/s and time to first token with and without draft; acceptance rate | at least 1.5x tokens/s over target alone, identical output at temperature 0 |
| E5 | Redundant execution for verification: 10 % of steps run twice at temperature 0, fixed seed | agreement rate between honest workers | at least 99 % byte-identical results on identical model builds; mismatch cases understood |
| E6 | Two coordinators forwarding steps to each other on a test network | forwarded-step success rate; credits booked once per step | zero double bookings over 1000 forwarded steps; no `trusted` step leaves a trusted node |
| E7 | Keep-alive on workers | per-job latency and idle RAM with `keep_alive` 0 vs. 5 min | latency gain documented; operator setting stays local, default unchanged |

Rules for all experiments: isolated test network for anything using `llama.cpp` RPC; no
public exposure; only approved models; results recorded with exact commands, without
prompts, tokens, IPs or hostnames.

---

## References

- Leviathan, Kalman, Matias (2023): Fast Inference from Transformers via Speculative Decoding.
- Chen et al. (2023): Accelerating Large Language Model Decoding with Speculative Sampling.
- Santilli et al. (2023): Accelerating Transformer Inference for Translation via Parallel
  Decoding (Jacobi decoding).
- Fu et al. (2024): Break the Sequential Dependency of LLM Inference Using Lookahead Decoding.
- Cai et al. (2024): Medusa: Simple LLM Inference Acceleration Framework with Multiple
  Decoding Heads.
- Borzunov et al. (2023): Petals: Collaborative Inference and Fine-tuning of Large Models.
- Wang et al. (2024): Mixture-of-Agents Enhances Large Language Model Capabilities.

---

## Zusammenfassung (Deutsch)

**Status:** Forschung und Vision, nicht implementiert. Heute gibt es einen Coordinator, ganze
Modelle pro Maschine, Vertrauensstufen und Credits. Es gibt keine Modellaufteilung, keine
Föderation, keine Handy-Worker und keine vertrauliche Inferenz.

- **Kernaussage:** Viele Geräte bringen Durchsatz, nicht Geschwindigkeit für eine einzelne
  Anfrage. Über weite Strecken Aufgaben zerlegen, nicht Modelle.
- **Handys:** Android ist machbar (erst Termux-Test, dann native App mit Regeln für Laden,
  WLAN und Temperatur). iOS nur im Vordergrund.
- **Aufgabenzerlegung:** Map-Reduce, Generieren und Prüfen, Abstimmung. Zwischen Schritten
  Klartext statt Token-IDs.
- **Föderation:** regionale Coordinatoren, Ursprungsknoten bleibt Eigentümer, signierte
  Quittungen und Verrechnung statt Blockchain, Worker holen weiterhin selbst ab.
- **Regionale Cluster:** Modellaufteilung nur bei wenigen Millisekunden Abstand; ein Cluster
  erscheint dem Coordinator als ein Worker.
- **Speculative Decoding:** kleines Ratemodell auf schwachem Gerät, Prüfung auf starkem Gerät
  oder Cluster.
- **Datenschutz:** Token-Aufteilung allein schützt keine Daten.
- **Nächste Schritte:** sieben Experimente (E1 bis E7) mit messbaren Erfolgskriterien.
