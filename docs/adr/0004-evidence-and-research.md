# ADR 0004: Capture bounded evidence before rewriting

## Status

Accepted.

## Context

Conversation-derived material often contains uncertain or time-sensitive claims.
Rewriting can improve prose while accidentally making unsupported claims sound
authoritative. The project requires recorded sources and review for uncertainty.

## Decision

Use Codex web search as the primary research adapter with a strict source JSON
schema, URL validation, citation metadata, and a bounded source count. If the
Codex research call is unavailable, use the deterministic HTTP adapter as a
fallback and record the failure. Search snippets are never treated as facts.

## Consequences

Research packets become inspectable inputs to rewriting. Official and primary
sources are preferred, but source presence does not remove editorial review for
medical, legal, financial, privacy, regulatory, or time-sensitive claims.

## Alternatives considered

HTTP-only research was predictable but weaker as the primary source-finding
path. Codex-only research had no deterministic availability fallback.
