# Ablation `ablation-n40-s1-scripted-qwen3-4b-instruct`

Agent model `ollama:qwen3:4b-instruct`, simulator `scripted`, 40 scenarios per config (stratified, seed 1), finished 2026-09-28 16:55 UTC.

| Config | Task success | Wrong-action rate | False 'done' claims | Esc. precision | Esc. recall | Injection success | Policy accuracy | p50 / p95 latency | ₹ / convo (hosted ref.) |
|---|---|---|---|---|---|---|---|---|---|
| Baseline (prompt only) | 40.0% | 2.5% | 2.5% | n/a | 0.0% | 25.0% | 22.2% | 1.3 s / 2.9 s | ₹0.06 |
| + policy engine | 47.5% | 0.0% | 0.0% | 100.0% | 14.3% | 25.0% | 22.2% | 1.1 s / 3.8 s | ₹0.06 |
| + guardrails | 52.5% | 0.0% | 0.0% | 100.0% | 14.3% | 0.0% | 22.2% | 1.1 s / 3.9 s | ₹0.06 |
| + RAG | 50.0% | 0.0% | 0.0% | 100.0% | 14.3% | 0.0% | 11.1% | 1.2 s / 4.4 s | ₹0.07 |
| + router (full) | 70.0% | 0.0% | 0.0% | 100.0% | 57.1% | 0.0% | 44.4% | 1.2 s / 3.7 s | ₹0.06 |
