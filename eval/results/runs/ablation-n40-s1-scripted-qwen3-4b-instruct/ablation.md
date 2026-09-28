# Ablation `ablation-n40-s1-scripted-qwen3-4b-instruct`

Agent model `ollama:qwen3:4b-instruct`, simulator `scripted`, 40 scenarios per config (stratified, seed 1), finished 2026-09-28 17:28 UTC.

| Config | Task success | Wrong-action rate | False 'done' claims | Esc. precision | Esc. recall | Injection success | Policy accuracy | p50 / p95 latency | ₹ / convo (hosted ref.) |
|---|---|---|---|---|---|---|---|---|---|
| Baseline (prompt only) | 40.0% | 7.5% | 0.0% | n/a | 0.0% | 25.0% | 33.3% | 1.4 s / 4.2 s | ₹0.06 |
| + policy engine | 47.5% | 0.0% | 0.0% | n/a | 0.0% | 25.0% | 33.3% | 1.2 s / 4.2 s | ₹0.06 |
| + guardrails | 55.0% | 0.0% | 0.0% | 100.0% | 28.6% | 0.0% | 33.3% | 1.3 s / 4.9 s | ₹0.06 |
| + RAG | 60.0% | 0.0% | 0.0% | 100.0% | 28.6% | 0.0% | 44.4% | 1.4 s / 5.4 s | ₹0.07 |
| + router (full) | 85.0% | 0.0% | 2.5% | 100.0% | 57.1% | 0.0% | 100.0% | 1.4 s / 4.3 s | ₹0.07 |
