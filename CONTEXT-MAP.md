# Context map

This map keeps the repository's durable context easy to route.

| Question | Read first | Then inspect |
| --- | --- | --- |
| What do project terms mean? | `CONTEXT.md` | `docs/TAXONOMY.md` |
| How does data flow? | `docs/architecture.md` | `PIPELINE.md`, `dags/google_takeout_articles.py` |
| Why is a provider or boundary shaped this way? | `docs/adr/` | `codex_provider.py`, `privacy_filter.py` |
| How are categories controlled? | `taxonomy.py` | `validate_content.py`, `migrate_taxonomy.py` |
| How is Takeout staged? | `docs/adr/0003-privacy-gated-takeout-intake.md` | `takeout_intake.py`, `activity_pipeline.py` |
| How are model calls authenticated? | `docs/adr/0001-codex-authenticated-model-provider.md` | `codex_provider.py`, `codex_gateway.py` |
| How are changes verified? | `AGENTS.md` | `tests/`, required checks in `PIPELINE.md` |

The Mermaid workflow in `docs/architecture.md` is the relationship projection;
the ADRs and source modules are authoritative for decisions and behavior.
