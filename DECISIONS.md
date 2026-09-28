# Decisions

Every non-obvious choice, with a one-line rationale. Deviations from the original brief are marked **(deviation)**.

## Money, time and locale
- **Money is stored as integer paise everywhere.** Binary floats cannot represent 0.10 exactly, so float rupees drift and boundary checks like "refund ≤ ₹2,000" become wrong at ₹2,000.00; integers make every comparison exact.
- **`rupees_to_paise` rejects floats outright.** It accepts `str`, `int` or `Decimal` only, so a float can never enter the money path by accident.
- **`format_inr` omits `.00` for whole rupees** (₹2,000, but ₹2,000.01). This reads naturally to Indian customers and still shows paise when they exist.
- **LLM cost is converted USD→INR and rounded *up* to whole paise per call.** Budgets must never under-count; the bias is under one paisa per call.
- **The budget day starts at midnight IST, and timestamps are stored in UTC.** Customers and operators think in IST, while storage stays unambiguous.

## LLM provider
- **Primary LLM: a local small model via Ollama (`qwen3:4b-instruct`)** **(deviation)**. There was no Anthropic key, the Hugging Face free credits ran out mid-build, and the owner asked for a local SLM. It costs ₹0 and keeps data on the machine.
- **`qwen3:4b-instruct` rather than `qwen3.5:4b` or `qwen3:4b`.**
  - Qwen3.5's hybrid linear-attention design gets no prompt-prefix cache in Ollama, which measured 16–35 s per call.
  - Plain `qwen3:4b` is the reasoning variant and emits about 350 reasoning tokens even with `think:false`.
  - The instruct variant caches the ~1,900-token prompt and runs about 6–10 s per call on CPU.
- **Hugging Face router and Anthropic implementations are kept.** The same `LLMClient` interface serves the hosted demo on Render, where there is no GPU or Ollama.
- **Hugging Face calls are priced from the router's live per-provider price list, keyed on the `x-inference-provider` header.** Costs come from real list prices instead of guesses.
- **The default HF model uses the `:cheapest` routing suffix.** Auto-routing picked a provider about 9× more expensive in testing.
- **Anthropic: `claude-opus-5` by default, with the `fallbacks: "default"` refusal fallback and cached system prompt and tools.** These follow the current API guidance; retries are disabled in the SDK because the agent loop owns retries (and traces them).
- **No FakeLLM in the application** **(deviation)**. The owner explicitly asked for real models. A scripted test double lives only in `tests/`, so unit tests never hit the network or spend credits.

## Agent orchestration
- **A hand-written loop, with no agent framework.** Every step is visible, traced and testable (brief requirement).
- **Tool results are wrapped as `<tool_output untrusted="true">`.** The model sees them as data, not instructions (injection layer 2).
- **Router: deterministic regex/keyword classification, not an LLM call.** It runs on every message, so it must be cheap and predictable, and it drives code-level decisions.
- **Router fast paths.**
  - *Auto-verify:* when a message contains an email, an order number and a 6-digit PIN, the loop calls `verify_customer` itself.
  - *Auto-retrieve:* for a confident `policy_question`, the loop fetches help-center passages before generating.
  - Small models often re-ask for details or answer from memory; the tools still perform the real checks, and the ablation measures the effect.
- **One-shot "announced work" nudge.** A reply like "Let me check that…" with no tool call triggers a single re-prompt. It is a common failure of small models; nudging only once bounds cost.
- **Output checks run before the reply is released.** That is why SSE streams progress events rather than tokens: tokens can't be un-sent if the check then rewrites the reply.
- **Unbacked "done" claims get one rewrite, then a deterministic safe reply.** Most drafts get fixed cheaply, and the fallback guarantees the customer is never told something false.
- **The handoff summary is deterministic, not LLM-written.** It can't hallucinate actions, costs nothing and is unit-testable.
- **Action cap: every action *attempt* counts, including denied ones.** This stops probing, such as trying many refund amounts. Real denials are reported with their true reason before the cap applies.
- **Verification can't switch to a different customer mid-chat.** This blocks harvesting several accounts in one session.
- **"Not your order" and "no such order" return identical errors.** Order numbers can't be enumerated.
- **A refund that needs approval returns `ok: false` with "NOT issued yet".** An `ok: true` next to money that hasn't moved invites the model to say "done".
- **Injection-flagged turns withhold action tools entirely.** A jailbroken model still cannot move money that turn; the customer can simply rephrase. This trades a small false-positive cost for safety.

## Policy
- **The refund window is inclusive: exactly 30×24 h after delivery is allowed; one second later is denied.** A single, testable definition.
- **Thresholds apply to the refund *amount*, not the order total.** A ₹598 partial refund on a ₹2,497 order is auto-approved.
- **COD refunds always need a human, with urgency raised above ₹10,000.** Bank details must never be collected in chat.
- **Denial explanations never state internal thresholds.** Otherwise customers could shape requests to sit just under a limit. The output check also blocks threshold leaks.

## Data, RAG, storage
- **SQLite locally and Postgres in production, through the same SQLAlchemy code.** A custom `UTCDateTime` type makes SQLite return timezone-aware datetimes.
- **`postgres://` URLs are rewritten to `postgresql+psycopg://`.** Render hands out the former; SQLAlchemy needs an explicit driver.
- **Seed data is deterministic and computed relative to "now"**; the eval pins "now" to 2026-06-15. The live demo gets fresh edge cases (delivered 29 vs 31 days ago) on the day it's seeded, while eval runs are reproducible forever.
- **The admin panel can reseed demo data.** Edge cases drift as days pass on a long-running demo.
- **KB chunks are one `##` section each, with the article title prefixed.** Sections are the natural unit of an FAQ answer, and citations stay precise.
- **The Chroma index is keyed by a SHA-256 of KB content plus the embedder name.** Startup is idempotent and rebuilds only when articles change.
- **A hashing embedder is used for tests and CI.** It needs no model download, is deterministic, and still does lexical retrieval.
- **The fastembed model is baked into the Docker image, and `HF_HUB_OFFLINE=1` is set.** Cold starts need no network.

## Privacy and security
- **Messages are stored raw, while traces, logs and every admin view are PII-masked.** The agent needs real history; nothing downstream does.
- **Over-masking is preferred.** Addresses are masked from the first comma-separated segment up to the PIN, and courier AWBs are generated as `AWB` plus 9 digits so they never look like Aadhaar numbers.
- **Client IPs are stored only as salted SHA-256 hashes.** Rate limiting and abuse analysis don't need raw IPs.
- **Rate limiting is an in-process sliding window, per IP (X-Forwarded-For aware) and per session.** It suits a single-instance demo; multiple replicas would need Redis or Postgres.
- **The admin panel is disabled when `ADMIN_PASSWORD` is empty**, and credentials are compared in constant time. It is safe by default.
- **The CSP is strict, with no inline scripts or styles.** The public demo accepts user text rendered in the page.

## Evaluation
- **Every scenario runs on a fresh in-memory DB seeded at a fixed time.** Scenarios are independent and reproducible, and the DB diff is the ground truth.
- **A pending-approval refund counts as escalation, not a wrong action.** No money moved, and a human decides.
- **An "answered_lookup" outcome was added to the four in the brief** **(deviation)**. Order-status questions are neither policy answers nor refusals.
- **Attack scenarios always use scripted payloads.** An LLM simulator might paraphrase the attack away; exact payloads make configs comparable.
- **The ablation grid uses the scripted simulator on a stratified subset; the main run uses the LLM simulator on all scenarios.** On a CPU-only SLM each conversation takes about a minute, so the grid holds customer turns constant across configs, which also makes the comparison fairer.
- **The LLM judge is the same local 4B model.** It is the only model available; its scores are secondary and reported as such.
- **Costs are reported two ways: actual (₹0 locally) and a hosted reference price.** The reference uses the measured token counts × the HF list price of Qwen3-235B, making the cost model visible without claiming it was paid.

## Deployment
- **Render region: Singapore.** It is the Render region closest to India; the alternatives are Oregon, Ohio, Virginia and Frankfurt.
- **Render plan `starter` rather than `free`.** Chroma, onnxruntime and the embedding model need more than the free plan's memory, and free instances sleep.
- **CI's smoke eval runs a 0.5B model through Ollama on the runner, gated on safety (0 successful injections) rather than quality.** It is the real loop end to end, needs no secrets, and the policy engine makes the gate model-independent.
