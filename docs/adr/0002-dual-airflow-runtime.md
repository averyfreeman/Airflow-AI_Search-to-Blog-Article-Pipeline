# ADR 0002: Support host and Docker Airflow through one provider seam

## Status

Accepted.

## Context

Standalone scripts run naturally on the host, while the existing production
workflow runs in Airflow containers. OAuth state should not be copied into each
worker, and the two modes should not develop different prompt or metadata
behavior.

## Decision

Use `CODEX_RUNTIME=host` for direct SDK execution and
`CODEX_RUNTIME=gateway` for Docker. The host gateway accepts only bearer-
authenticated, bounded JSON generation requests and delegates to the same
Codex provider and fallback policy. Airflow pools call the `codex` pool.

## Consequences

Provider selection is configuration, not a fork of every script. Container
workers can use the same exact model, prompt policy, privacy gate, research
mode, and audit metadata. The gateway is an additional process to supervise and
its host token must be provisioned before Docker runs.

## Alternatives considered

Host-only execution was simpler but did not support the existing Compose
deployment. Docker-only login was rejected because interactive OAuth lifecycle
inside workers is fragile and unnecessarily duplicates account state.
