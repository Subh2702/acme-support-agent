# PLAN — Acme Outdoors autonomous support agent

Each phase ends with: tests + ruff green → git commit. The next phase starts only after that.

## Phase 1 — Skeleton, config, money, DB, seed, health
- [ ] `app/config.py` (pydantic-settings, every env var documented in `.env.example`)
- [ ] `app/money.py` — paise ⇄ INR (Decimal only), `format_inr` with Indian grouping, USD→INR cost conversion
- [ ] `app/db/` — SQLAlchemy 2.x models (customers, orders, items, refunds, tickets, conversations, messages, trace_events), session factory (SQLite + Postgres)
- [ ] `app/db/seed.py` — deterministic, idempotent: 40 customers, ~150 orders, named edge-case fixtures
- [ ] `/healthz`, `/readyz`
- [ ] Tests: money, seed determinism/idempotency, health

## Phase 2 — Policy engine and tools
- [ ] `app/policy/engine.py` — pure functions → `ALLOW | DENY | NEEDS_APPROVAL` + reason code; thresholds in one `PolicyConfig` (paise)
- [ ] `app/tools/` — Pydantic arg models, JSON schemas, `{ok, data | error_code, message}` results, policy enforced inside every action tool
- [ ] Tests: every rule and boundary (day 29/30/31, ₹2,000 vs ₹2,000.01, ₹10,000 vs ₹10,000.01, COD, ownership, action cap), tool tests

## Phase 3 — LLM client, agent loop, router, state, tracing
- [ ] `app/llm/` — `LLMClient` protocol, Hugging Face impl (primary, real model), Anthropic impl, cost accounting in paise from real provider prices
- [ ] `app/agent/` — hand-written tool loop (max iterations, per-tool timeouts, retry/backoff, fallback), state, router, prompts
- [ ] `trace_events` logging for every LLM call / tool call / policy decision / guardrail hit
- [ ] Tests: loop with a scripted test double (tests/ only, no network), retries, timeouts; live smoke test against HF

## Phase 4 — RAG
- [ ] 15–25 help-center articles (Indian context) in `app/rag/kb/*.md`
- [ ] Idempotent Chroma index (content hash), fastembed embeddings, hashing embedder for hermetic tests
- [ ] Tests: chunking, idempotency, retrieval quality on known questions

## Phase 5 — Guardrails, escalation, handoff
- [ ] Injection heuristics (English + Hinglish), action-tool gating on suspected turns
- [ ] PII masking (email, +91/10-digit phones, cards, Aadhaar-like, PAN, UPI, addresses with PIN)
- [ ] Output checks (no unbacked action claims, no prompt/threshold leak, no other-customer data)
- [ ] Automatic escalation triggers + deterministic handoff summary
- [ ] Rate limiting (IP + session), daily ₹ budget cap
- [ ] Prompt-injection regression test (injection cannot cause a policy-denied refund)

## Phase 6 — API and UI
- [ ] `POST /api/chat`, `POST /api/chat/stream` (SSE progress events), demo info
- [ ] Chat UI (mobile-friendly, typing indicator, escalation banner, sample prompts + credentials)
- [ ] Admin panel (basic auth): conversations, trace timeline, escalation queue, eval results, cost/latency stats (₹)
- [ ] API integration tests

## Phase 7 — Evaluation
- [ ] Customer simulator (LLM personas + scripted fallback)
- [ ] 100+ YAML scenarios
- [ ] Deterministic DB/trace judges + LLM tone/faithfulness judge
- [ ] Runner: configs, ablations, resumable, `--smoke`; results JSON + markdown
- [ ] Real eval run recorded in `eval/results/`

## Phase 8 — Ship
- [ ] Multi-stage Dockerfile (non-root, healthcheck), docker-compose (app + Postgres)
- [ ] GitHub Actions CI (ruff, pytest+coverage, docker build, smoke eval)
- [ ] `render.yaml` (Singapore region), `DEPLOY.md`
- [ ] README, ARCHITECTURE.md, DECISIONS.md
