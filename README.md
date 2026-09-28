# Acme Outdoors — autonomous customer-support agent

An end-to-end support agent for a fictional Indian outdoor-gear store. It answers policy questions from a help center (RAG), looks up orders, and **takes real actions**: cancellations, refunds and address changes. It escalates to a human, with a structured handoff, when it should. The engineering focus is **safe action-taking**:

- Every business rule lives in a deterministic policy engine that runs *inside* each tool, so no phrasing, jailbreak or hallucination can move money it shouldn't.
- Layered guardrails catch injection, PII leaks and "I've refunded you" claims that never happened.
- A **simulation-based eval** (116 scenarios, 9 personas, deterministic DB-state judges) measures what each layer is worth.

All money is in ₹, stored as integer paise; timestamps are stored in UTC and shown in IST. It runs fully locally on a **4B-parameter model on a laptop GPU** (Ollama, RTX 4060 via Vulkan; CPU works too, just slower), and can switch to Hugging Face or Anthropic for hosting.

![demo](docs/demo.gif)

> **Demo GIF placeholder.** To record it: start the app (`uvicorn app.main:app`), open http://localhost:8000, click "Use these details" for Priya, then ask *"Refund AO-10001, the beanie didn't fit"*, then *"Also refund AO-10020"* (another customer's order, which must be refused), then *"I want to talk to a human"*. Record with Peek or OBS at 1280×800, keep it under 30 s, export to `docs/demo.gif` (e.g. `ffmpeg -i demo.mp4 -vf "fps=12,scale=960:-1" docs/demo.gif`). Then open `/admin` to show the trace timeline and the escalation card.

## Architecture

```mermaid
flowchart LR
    UI[Chat UI] -->|SSE| API[FastAPI]
    API --> RL[Rate limits] --> Loop[Agent loop]
    Loop --> G1[Budget ₹ · injection heuristics]
    Loop --> R[Router: intent · sentiment · language<br/>auto-escalation · fast paths]
    Loop <--> LLM[(LLMClient:<br/>Ollama SLM · HF · Anthropic)]
    Loop --> T[Typed tools]
    T --> P[Policy engine<br/>ALLOW · DENY · NEEDS_APPROVAL]
    T --> DB[(SQLite / Postgres)]
    T --> KB[RAG: Chroma + fastembed]
    Loop --> OC[Output checks:<br/>unbacked claims · leaks · foreign data]
    Loop --> TR[(trace_events, PII-masked)]
    T --> TK[(Tickets + handoff summary)]
```

The request lifecycle, the policy engine design, the threat model and the eval methodology are covered in [ARCHITECTURE.md](ARCHITECTURE.md), and every non-obvious choice is explained in [DECISIONS.md](DECISIONS.md).

## Quickstart

### Local (Python 3.12 + Ollama)
```bash
uv venv --python 3.12 .venv && uv pip install -r requirements.txt -r requirements-dev.txt
ollama pull qwen3:4b-instruct            # 2.5 GB
OLLAMA_VULKAN=1 ollama serve &           # GPU via Vulkan (omit the env var for CPU-only)
cp .env.example .env                     # set ADMIN_PASSWORD
.venv/bin/uvicorn app.main:app --reload  # http://localhost:8000  ·  /admin
```
First start seeds the DB (40 customers, 150 orders), downloads the 65 MB embedding model and builds the KB index. To use a hosted model instead, set `HF_TOKEN` (Hugging Face router) or `LLM_API_KEY` (Anthropic); see `.env.example`.

### Docker
```bash
cp .env.example .env
docker compose up --build                                            # app + Postgres, host Ollama
OLLAMA_BASE_URL=http://ollama:11434 docker compose --profile ollama up --build   # Ollama in a container
```

### Tests, lint, eval
```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/pytest --cov=app                                   # all LLM calls stubbed
.venv/bin/python -m eval.run_eval --smoke                    # 10 scenarios
.venv/bin/python -m eval.run_eval --config full --n 100 --seed 1
.venv/bin/python -m eval.run_eval --config ablation --n 40 --simulator scripted --no-judge
```

## Demo credentials
The agent verifies identity with **email + order number + delivery PIN code**.

| Customer | Email | Order | PIN | Try |
|---|---|---|---|---|
| Priya Sharma | priya.sharma@example.in | AO-10001 | 560034 | refund AO-10001 (auto-approved), cancel AO-10002, refund AO-10005 (31 days, refused) |
| Rahul Verma | rahul.verma@example.in | AO-10006 | 110017 | refund AO-10006 (₹8,999 → human approval), AO-10007 (₹32,999 → high urgency) |
| Ananya Iyer | ananya.iyer@example.in | AO-10010 | 600020 | COD refund (always a human, no bank details in chat), cancel AO-10011 |

Admin: `/admin`, user `admin`, password from `ADMIN_PASSWORD`.

## Results

<!-- RESULTS -->

## What failed and how I fixed it

Real problems hit while building this, roughly in order:

1. **Hosted LLM credits ran out mid-build.** The Hugging Face free tier returned `402` after a handful of calls. *Fix:* added an Ollama client and moved to a local 4B model on the CPU. Cost dropped to ₹0 per conversation; the HF and Anthropic clients stay for hosting.
2. **Every call re-processed the whole prompt, at 16–35 s per call.** Qwen3.5's hybrid linear attention gets no prompt-prefix cache in Ollama. *Fix:* switched to `qwen3:4b-instruct`, a standard-attention model, and measured an ~1,900-token prompt served from cache.
3. **The "instruct" model I first pulled was the reasoning variant.** It wrote about 350 tokens of chain-of-thought into every reply despite `think:false`. *Fix:* used the `-instruct` tag.
4. **The cache still missed on every turn.** Qwen's chat template renders tool schemas *after* the system message, and I had put the per-turn context (time, identity) inside it. *Fix:* moved the context to a trailing system message. Cached tokens went from 782 to ~1,750 of ~2,100, and turns got about 2.5× faster.
5. **The small model re-asked for details the customer had already given, and answered policy questions from memory.** *Fix:* router fast paths. Auto-verify runs when email, order number and PIN are all present, and auto-retrieve fetches KB passages for confident policy questions. The tools still perform the real checks.
6. **"Let me check that for you…" with no tool call.** *Fix:* a one-shot nudge. A context note wasn't enough for a 4B model; the working version puts the draft plus an explicit automated message back into the in-turn conversation.
7. **The model misread dates** ("your refund window has closed" on a 10-day-old delivery). *Fix:* `get_order` now returns eligibility computed by a policy-engine dry run, phrased as the exact next tool call. It shows decisions, never thresholds.
8. **Leaks found by my own tests:** the customer's full name reached the admin trace through a tool *message*, and an example order number like `AO-12345` was redacted as if it belonged to another customer. *Fix:* the name moved into structured, masked data, and redaction now applies only to real orders owned by someone else.
9. **Hinglish false positive:** "fir se try karta hoon" ("I'll try again") was flagged as a legal threat, because FIR is also a police report. *Fix:* legal patterns now require FIR *filing* verbs, backed by a regression test.
10. **A flaky backoff test.** Additive jitter could exceed exponential growth at small bases. *Fix:* multiplicative jitter (×1.0–1.25), which is strictly increasing, rerun 15×.
11. **The GPU wasn't used at first.** The NVIDIA DKMS module build had been interrupted during a system update, so no kernel module existed for the running kernel and Ollama fell back to the CPU. *Fix:* rebuilt the module with DKMS and started Ollama with its Vulkan backend (`OLLAMA_VULKAN=1`; this install ships no CUDA runner). Generation went from ~9 to ~70 tok/s, and a whole eval scenario from 30–90 s to a few seconds.
12. **Process hygiene:** I committed once with a failing test (a pipe hid pytest's exit code) and once with a lint error. *Fix:* a `set -euo pipefail` quality gate before every commit, and the bad commits were amended locally before anything was pushed.


## Limitations
- **Model capability.** A 4B model on a CPU is the binding constraint on task success: it sometimes over-confirms, explains a process instead of acting, or quotes wrong facts. The safety layers keep that from turning into *wrong actions*, but they can't make it helpful. See "Future work" for the one-line switch to a hosted model.
- **Latency numbers are for one laptop GPU** (RTX 4060, 8 GB, Vulkan) serving one conversation at a time. Concurrent users would queue on a single Ollama instance.
- **The LLM judge is the same 4B model.** Tone, helpfulness and faithfulness scores are secondary and noisy; the deterministic judges are the primary signal.
- **The ablation grid used 40 stratified scenarios with a scripted customer** (the main run uses all 116 with an LLM customer). Differences of a few points between configs are within noise at that size.
- **Low-value false non-delivery claims** are routed to a human by the prompt, not by code. The policy engine would allow a ₹899 refund on a delivered order if the model called the tool.
- **Rate limiting and budget caching are per process.** They are fine for one instance; multiple replicas need Redis/Postgres-backed counters.
- **Identity is a knowledge check** (email + order + PIN), not authentication. A real deployment would use OTP login.
- **No live public URL yet:** no hosting CLI was available here. [DEPLOY.md](DEPLOY.md) has the ~15-minute Render steps, and the demo needs an LLM credential with credits.


## Future work
- Run the full eval on a stronger hosted model (`HF_TOKEN` with credits, or `LLM_API_KEY`) and compare with the local SLM. It is the same command with a different env var.
- Serve the local model with a batching server (vLLM/llama.cpp server with parallel slots) for concurrent users.
- An amount-faithfulness output check: every ₹ figure in a reply must appear in tool data from this conversation.
- Route low-value non-delivery claims through a specialist in code, not just in the prompt (currently a prompt-level rule).
- Redis-backed rate limiting and budget counters for multi-replica deployments.
- OTP-based customer login instead of the email + order + PIN knowledge check.
- Human-in-the-loop console: approve or reject pending refunds from the admin panel, with a write-back to the conversation.
- Nightly eval in CI against a hosted model, with trend charts in the admin panel.
