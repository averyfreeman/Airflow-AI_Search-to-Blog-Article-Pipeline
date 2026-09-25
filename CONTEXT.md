# Project context

This glossary is the shared vocabulary for the Takeout-to-article pipeline.
Implementation decisions live in `docs/adr/`; this file intentionally stays
terminological rather than procedural.

- **Takeout request** — A manually requested Google Takeout export containing
  one or more dated snapshots.
- **Activity record** — One source event from `MyActivity.json`, identified by
  a stable content-derived record ID.
- **Intake** — The controlled handoff from raw Takeout storage into an ignored
  processing inbox.
- **Staging article** — A normalized or rewritten Markdown candidate that has
  not yet crossed the publication boundary.
- **Privacy gate** — The deterministic redaction and high-risk blocking step
  that runs before model processing.
- **Provider result** — Model output plus non-content audit metadata such as
  provider, requested/actual model, reasoning effort, run ID, and hashes.
- **Codex runtime** — The local Codex app-server or its bearer-authenticated
  host gateway, with ChatGPT-managed authentication.
- **Editorial review** — A human decision point required for privacy,
  uncertainty, time-sensitive, regulatory, or low-confidence content.
- **Research packet** — A bounded, source-linked evidence record used to
  verify claims during rewriting.
- **Publication boundary** — The validator-backed atomic move into the
  category-slug article tree.
- **Run ledger** — JSONL records that make selection, routing, fallback, and
  publication decisions inspectable and repeatable.
