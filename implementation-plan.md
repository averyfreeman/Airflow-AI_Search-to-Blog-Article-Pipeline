# Implementation plan

This projection is derived from Matt-native context and ADR documents.

## Problem

Turn manually acquired Google Takeout activity into privacy-gated, evidence-backed educational articles with auditable autonomy.

## Languages

python

## Decisions

- [ADR 0001: Use Codex app-server authentication for model work](docs/adr/0001-codex-authenticated-model-provider.md) — 
- [ADR 0002: Support host and Docker Airflow through one provider seam](docs/adr/0002-dual-airflow-runtime.md) — 
- [ADR 0003: Redact and gate Takeout content before model calls](docs/adr/0003-privacy-gated-takeout-intake.md) — 
- [ADR 0004: Capture bounded evidence before rewriting](docs/adr/0004-evidence-and-research.md) — 
- [ADR 0005: Keep taxonomy and publication decisions deterministic](docs/adr/0005-publication-boundary.md) — 
