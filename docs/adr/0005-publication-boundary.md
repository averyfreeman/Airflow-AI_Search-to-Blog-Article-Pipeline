# ADR 0005: Keep taxonomy and publication decisions deterministic

## Status

Accepted.

## Context

The model can suggest a topic or article angle, but a model-created folder or
tag would violate the controlled taxonomy. Publication also needs a reversible,
auditable boundary.

## Decision

`taxonomy.py` remains the only category authority. The article surgeon may
recommend a split but does not control category folders. Every published file
must have exactly one allowlisted Category and exactly one matching Tags value,
with the category slug as its parent. Strict validation runs before atomic
publication. High-confidence ordinary rewrites may auto-publish; privacy,
uncertainty, time-sensitive/regulatory, ambiguous, and explicitly exempt
content routes to review.

## Consequences

The model has useful autonomy over prose and prioritization without authority to
expand the publication taxonomy or bypass safety gates. Migration manifests,
score records, rewrite backups, research packets, and disposition ledgers make
the path inspectable and provide override points.

## Alternatives considered

Fully automatic publication was rejected because a polished unsupported claim
is more dangerous than a rough draft. Manual approval for every ordinary
article was rejected because it would prevent useful bounded autonomy.
