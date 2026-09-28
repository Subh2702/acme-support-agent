# Ablation `ablation-n40-s1-scripted-qwen3-4b-instruct`

Agent model `ollama:qwen3:4b-instruct`, simulator `scripted`, 40 scenarios per config (stratified, seed 1), finished 2026-09-28 17:11 UTC.

| Config | Task success | Wrong-action rate | False 'done' claims | Esc. precision | Esc. recall | Injection success | Policy accuracy | p50 / p95 latency | ₹ / convo (hosted ref.) |
|---|---|---|---|---|---|---|---|---|---|
| Baseline (prompt only) | 42.5% | 2.5% | 0.0% | n/a | 0.0% | 25.0% | 33.3% | 1.2 s / 4.5 s | ₹0.06 |
| + policy engine | 50.0% | 0.0% | 0.0% | n/a | 0.0% | 25.0% | 33.3% | 1.2 s / 4.1 s | ₹0.06 |
| + guardrails | 50.0% | 0.0% | 0.0% | n/a | 0.0% | 0.0% | 33.3% | 1.2 s / 4.3 s | ₹0.06 |
| + RAG | 57.5% | 0.0% | 0.0% | 100.0% | 14.3% | 0.0% | 33.3% | 1.4 s / 4.1 s | ₹0.07 |
| + router (full) | 85.0% | 2.5% | 2.5% | 100.0% | 57.1% | 0.0% | 100.0% | 1.3 s / 4.2 s | ₹0.07 |
