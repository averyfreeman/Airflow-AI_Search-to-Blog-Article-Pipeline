# ADR 0003: Redact and gate Takeout content before model calls

## Status

Accepted.

## Context

Google Takeout records can contain identifiers, credentials, and sensitive
health, legal, financial, or employment context. Raw records must remain in
ignored local storage and should not be sent to a model by accident.

## Decision

Manual Takeout acquisition remains outside the pipeline. `takeout_intake.py`
copies into ignored `inbox/` only when explicitly applied and writes a checksum
manifest. The privacy filter redacts common identifiers. Secrets, government
IDs, and high-risk sensitive contexts block model calls by default. An override
must include both an explicit enable flag and a reason, is recorded as a safe
metadata field, and still routes the resulting article to editorial review.

## Consequences

The source tree is never deleted or modified by intake. Model prompts and
research inputs receive sanitized content. Some legitimate technical examples
may require an operator-reviewed override; that trade-off is visible in the
ledger rather than hidden in a prompt.

## Alternatives considered

Sending raw records was rejected for privacy reasons. Blocking every record
with an identifier was rejected because it would discard ordinary technical
material unnecessarily.
