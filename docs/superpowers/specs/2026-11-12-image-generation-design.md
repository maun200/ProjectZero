# Image generation in the grid

## Intent and why

The grid currently only serves text chat (Ollama LLMs). The operator wants the same grid to
generate images from a text prompt, consistent with the project's existing "uncensored, locally
hosted" philosophy that already applies to the chat models (abliterated Qwen2.5 family, no
refusal-tuning). Video generation and sending images *to* the AI for analysis (vision) were
both explicitly raised alongside this request but are intentionally **out of scope for this
design** — the operator chose to do image generation first and revisit the other two later.

## Goals

- A member can type a text prompt in the app and get a generated image back, through the same
  job queue/credit/worker-trust machinery already used for chat (no parallel system).
- The image checkpoint is an uncensored community model (no baked-in NSFW safety-checker),
  mirroring the decision already made for the chat models.
- Only the desktop (i9-9900K + GTX 1660, 6 GB VRAM) is expected to actually run this — it is the
  only GPU in the grid with enough VRAM. CPU-only boxes (CT 100, dell-optiplex3070,
  ubuntu-hp-laptop, fedora-laptop) are not expected to generate images in reasonable time, and
  the design must not require them to.
- Generated images are stored and shown the same unencrypted-on-the-server way chat text already
  is (README/SECURITY.md already say prompts are plaintext on the server; this does not change
  that posture, just extends it to images).

## Non-goals

- Video generation (deferred, revisit after image generation is working and proven on this
  hardware).
- Vision / image understanding (sending an image *to* the model for analysis) — deferred,
  revisit after image generation.
- A generic "plugin" system for arbitrary new job/media types. Build the one new kind (image)
  concretely; a future video or vision kind can follow the same pattern without needing this to
  be abstracted up front.
- Running image generation well on CPU-only or <6 GB VRAM workers. Not a target for this round.
- A new standalone image gallery/moderation/retention feature. Images live in the existing job
  history like any other job result; no new browsing UI beyond showing it inline in the chat.

## Anti-goals (would make this a failure even if "working")

- Silently falling back to a filtered/safety-checked model "to be safe" — the whole point is
  parity with the uncensored chat models.
- Making CPU-only workers advertise the image model and then time out/fail constantly. If a
  device can't realistically do this, it must not be offered the job at all.
- Bloating the SQLite job table so badly that normal chat jobs' queries slow down. Needs a sanity
  check once real image sizes are known (see "Open questions for the builder").

## Constraints

- Hardware: exactly one usable GPU in the whole grid for this (GTX 1660, 6 GB VRAM). Must pick a
  checkpoint/resolution that actually fits and generates in a tolerable time on that card —
  not aspirational SDXL-at-1024px speeds.
- Must fit the existing worker model: pull-based, worker never executes remote code, the
  coordinator never reaches into a worker. An image backend is a new *local* service the
  operator installs on the image-capable worker, analogous to how Ollama is installed for chat —
  not something the coordinator pushes or runs for them.
- Must fit the existing trust/privacy and credit mechanics (`trusted`/`community`, demo credits)
  rather than inventing a parallel accounting path.
- `CONTRIBUTING.md` scope rules still apply: no real model downloads in tests, no secrets/weights
  committed, worker still never runs attacker-controlled code.

## Decisions already made (by the operator, in this conversation)

- Image generation comes before vision or video.
- Video generation is explicitly deferred, not designed for now.
- The checkpoint must be an uncensored community model, not stock Stable Diffusion with its
  default safety-checker — same reasoning as the existing abliterated chat models.
- Only the desktop's GPU is expected to serve this; no attempt to make it work acceptably on the
  CPU-only boxes.
- **No content filter at all, including no special handling for sexualized depictions of
  minors.** A design-review pass (2026-11-12) explicitly flagged that generated visual content
  carries materially different and more severe legal exposure in most jurisdictions than
  uncensored text, independent of operator intent, and asked whether that distinction had been
  weighed before extending the "uncensored like the chat models" decision to images. The
  operator was shown this distinction directly, offered a middle option (no general NSFW filter,
  but a narrow hard block specifically against sexualized-minor content), and chose full parity
  with the chat models with no filter of any kind. This is recorded here as an informed decision
  made with the risk distinction stated plainly, not an oversight.

## Resolved architecture questions (from the 2026-11-12 design review)

A review pass surfaced two places where the doc's "builder's call" framing was too loose to
actually plan from, since the goal itself only works one specific way. Resolved here rather than
left open:

- **GPU sharing between the chat worker and the image worker on the desktop.** The desktop
  already runs a chat worker with the Ollama model fully GPU-resident (`num_gpu=99`, see
  `docs/BENCHMARK_2026-11-03.md`). A second, independent image-generation worker process on the
  same box would race it for the same 6 GB VRAM. Resolved: run the image backend so it loads the
  checkpoint into VRAM **only for the duration of a single generation** and releases it
  immediately after, rather than keeping it resident. This trades a per-request load delay for
  never permanently reserving VRAM against the chat model, and needs no cross-process locking.
  If VRAM is briefly unavailable (chat mid-generation at the same moment), the image job should
  fail cleanly and let the existing retry/requeue path (`requeue_or_fail`) try again rather than
  hang or crash the process. This is required for the stated goal to be true at all: the
  coordinator is pull-only and never pushes to workers, so nothing could flip a single shared
  worker between "chat mode" and "image mode" automatically — self-service image generation
  needs two independently-polling worker identities on the desktop, not one.
- **Fairness with exactly one image-capable device.** Unlike chat (where a busy desktop still
  leaves slower CPU-only fallbacks for the same model), there is no fallback device for images at
  all, so one member queuing several prompts in a row can lock out everyone else's image
  requests for the full duration. Resolved: cap outstanding image jobs at **1 per member** at a
  time (reject or queue-block a second submission from the same member until their first
  finishes). A simple guard rail, easy to loosen later; not meant to be the final word on
  fairness if the grid grows more members.

## What's left to the builder (my own calls, to make during planning/implementation)

These are implementation choices, not value judgments — sensible defaults I'll make and state
plainly, open to correction:

- **Which checkpoint.** Likely a popular SD1.5-based anime/general-purpose community checkpoint
  (small enough to fit 6 GB comfortably, fast enough to be usable) rather than an SDXL finetune
  (heavier, would be slow/tight on 6 GB). Exact model name decided at implementation time by
  checking what's actually available and well-regarded, the same way `qwen2.5-abliterate` sizes
  were picked earlier in this project.
- **Local image backend.** A small FastAPI wrapper around the `diffusers` library (own code,
  same spirit as `worker/mock_llm.py`'s existing mock pattern) rather than installing a full
  third-party WebUI (AUTOMATIC1111/ComfyUI). Keeps the "operator-installed local service with a
  simple, known API" property the chat side already has with Ollama, instead of depending on a
  large external project's API surface.
- **Routing image jobs to the right worker.** Reuse the *existing* `model` string matching
  (`jobs.model` / `workers.model`, already exact-match in `/api/worker/claim`) rather than adding
  a parallel "job kind" concept — give the image checkpoint a model name the operator approves
  via `CAG_ALLOWED_MODELS` like any other model, and only the desktop ever registers that model.
  This reuses the trust/allowlist machinery that already exists instead of building a new one.
- **Request/response shape.** A new, small `ImageJobIn`-style payload (prompt, optional
  negative-prompt/size/steps/seed) alongside the existing chat `JobIn`, and the result stored as
  base64 PNG in the existing `result_json` column (same column chat answers already use, just a
  different shape inside it) rather than introducing file storage on disk. Simplest change that
  reuses 90% of the existing job table, queue, lease, and credit code as-is.
- **Cost.** Image jobs should probably cost more demo credits than the flat `COST = 5` text jobs,
  since they're far more compute-intensive. Exact number is a tuning knob, not a design decision.
- **UI.** A minimal way to ask for an image (e.g. a model/mode picker that already exists in the
  chat composer) and render the returned base64 image inline in the chat log, reusing the
  existing message-rendering code path rather than a separate screen.

## Open questions for the builder to actually check before/while implementing

- Real generated-image file size at the chosen resolution/steps, to confirm base64-in-SQLite
  doesn't become a real problem (the anti-goal above) — if a single image routinely runs several
  MB, storing on disk with a path reference may be needed instead, which would change the
  "Result storage" call above.
- Actual generation time on the GTX 1660 for the chosen checkpoint/resolution/step count, to set
  a sane job queue timeout for image jobs (the existing `CAG_QUEUE_TIMEOUT_SECONDS`/lease timing
  is tuned for fast text jobs, not slower image jobs).
