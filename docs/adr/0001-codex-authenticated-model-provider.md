# ADR 0001: Use Codex app-server authentication for model work

## Status

Accepted.

## Context

The pipeline needs an OpenAI model while avoiding direct handling of API keys or
private Codex credential caches. The requested model is `gpt-5.6-luna` with
reasoning effort `max`. The existing scripts used local OpenAI-compatible HTTP
and an OpenRouter scorer.

## Decision

Use the official `openai-codex` Python SDK to launch an ephemeral, read-only
Codex app-server turn. Let Codex own ChatGPT OAuth. The shared provider seam
records requested model, actual model, reasoning effort, run ID, prompt/output
hashes, provider, and fallback reason. Docker workers call a narrow host-side
gateway instead of receiving OAuth state.

## Consequences

- The exact requested model and effort are explicit in code and environment.
- The pipeline does not read `~/.codex/auth` or make a direct API call with an
  extracted OAuth token.
- A local OpenAI-compatible server remains an explicit fallback and is never
  silently presented as Codex.
- The gateway token is an operator-managed secret and must not enter logs or
  manifests.

## Alternatives considered

Direct API keys were rejected because credential lifecycle and scope would move
into this repository. GPT Actions were rejected because their OAuth flow
authenticates users to third-party action APIs rather than providing local
Python access to an OpenAI model.
