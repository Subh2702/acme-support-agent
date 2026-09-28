# PLAN — Acme Outdoors autonomous support agent

Each phase ends with: tests + ruff green → git commit. The next phase starts only after that.

## Phase 1 — Skeleton, config, money, DB, seed, health
- [x] `app/config.py` (pydantic-settings, every env var documented in `.env.example`)
- [x] `app/money.py` — paise ⇄ INR (Decimal only), `format_inr` with Indian grouping, USD→INR cost conversion
- [x] `app/db/` — SQLAlchemy 2.x models (customers, orders, items, refunds, tickets, conversations, messages, trace_events), session factory (SQLite + Postgres)
- [x] `app/db/seed.py` — deterministic, idempotent: 40 customers, ~150 orders, named edge-case fixtures
- [x] `/healthz`, `/readyz`
- [x] Tests: money, seed determinism/idempotency, health

## Phase 2 — Policy engine and tools
- [x] `app/policy/engine.py` — pure functions → `ALLOW | DENY | NEEDS_APPROVAL` + reason code; thresholds in one `PolicyConfig` (paise)
- [x] `app/tools/` — Pydantic arg models, JSON schemas, `{ok, data | error_code, message}` results, policy enforced inside every action tool
- [x] Tests: every rule and boundary (day 29/30/31, ₹2,000 vs ₹2,000.01, ₹10,000 vs ₹10,000.01, COD, ownership, action cap), tool tests

## Phase 3 — LLM client, agent loop, router, state, tracing
- [x] `app/llm/` — `LLMClient` protocol, Hugging Face impl (primary, real model), Anthropic impl, cost accounting in paise from real provider prices
- [x] `app/agent/` — hand-written tool loop (max iterations, per-tool timeouts, retry/backoff, fallback), state, router, prompts
- [x] `trace_events` logging for every LLM call / tool call / policy decision / guardrail hit
- [x] Tests: loop with a scripted test double (tests/ only, no network), retries, timeouts; live smoke test against HF

## Phase 4 — RAG
- [x] 15–25 help-center articles (Indian context) in `app/rag/kb/*.md`
- [x] Idempotent Chroma index (content hash), fastembed embeddings, hashing embedder for hermetic tests
- [x] Tests: chunking, idempotency, retrieval quality on known questions

## Phase 5 — Guardrails, escalation, handoff
- [x] Injection heuristics (English + Hinglish), action-tool gating on suspected turns
- [x] PII masking (email, +91/10-digit phones, cards, Aadhaar-like, PAN, UPI, addresses with PIN)
- [x] Output checks (no unbacked action claims, no prompt/threshold leak, no other-customer data)
- [x] Automatic escalation triggers + deterministic handoff summary
- [x] Rate limiting (IP + session), daily ₹ budget cap
- [x] Prompt-injection regression test (injection cannot cause a policy-denied refund)

## Phase 6 — API and UI
- [x] `POST /api/chat`, `POST /api/chat/stream` (SSE progress events), demo info
- [x] Chat UI (mobile-friendly, typing indicator, escalation banner, sample prompts + credentials)
- [x] Admin panel (basic auth): conversations, trace timeline, escalation queue, eval results, cost/latency stats (₹)
- [x] API integration tests

## Phase 7 — Evaluation
- [x] Customer simulator (LLM personas + scripted fallback)
- [x] 100+ YAML scenarios (116)
- [x] Deterministic DB/trace judges + LLM tone/faithfulness judge
- [x] Runner: configs, ablations, resumable, `--smoke`; results JSON + markdown
- [x] Real eval run recorded in `eval/results/`

## Phase 8 — Ship
- [x] Multi-stage Dockerfile (non-root, healthcheck), docker-compose (app + Postgres)
- [x] GitHub Actions CI (ruff, pytest+coverage, docker build, smoke eval)
- [x] `render.yaml` (Singapore region), `DEPLOY.md`
- [x] README, ARCHITECTURE.md, DECISIONS.md

## Deviations (see DECISIONS.md)
- LLM: local SLM via Ollama (qwen3:4b-instruct) as primary; Hugging Face + Anthropic clients kept for hosting. No FakeLLM in the app (owner's request); scripted test double in tests/ only.
- Added an `answered_lookup` eval outcome for order-status questions.
