"""Deterministic privacy filtering before content reaches a model provider.

The filter is intentionally conservative.  It redacts common identifiers and
secrets, blocks high-risk material by default, and never includes matched raw
values in findings or logs.  An override is explicit and must carry a reason;
callers remain responsible for deciding whether publication is appropriate.
"""

from __future__ import annotations

import dataclasses
import os
import re


@dataclasses.dataclass(frozen=True)
class PrivacyFinding:
    kind: str
    action: str
    count: int


@dataclasses.dataclass(frozen=True)
class PrivacyDecision:
    text: str
    findings: tuple[PrivacyFinding, ...]
    blocked: bool
    override_used: bool = False
    override_reason: str | None = None

    @property
    def risk(self) -> bool:
        return bool(self.findings)


_REDACTIONS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S), "[REDACTED PRIVATE KEY]"),
    ("api_key", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,})\b"), "[REDACTED API KEY]"),
    ("secret_assignment", re.compile(r"(?i)\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|password|secret)\s*[:=]\s*[^\s,;]+"), "[REDACTED SECRET]"),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED GOVERNMENT ID]"),
    ("email", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I), "[REDACTED EMAIL]"),
    ("phone", re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)"), "[REDACTED PHONE]"),
    ("credit_card", re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"), "[REDACTED PAYMENT NUMBER]"),
)

_HIGH_RISK_TERMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("health_context", re.compile(r"\b(?:diagnosis|medical record|patient|symptom|prescription|disability|workers?\s*comp|mental health|therapy)\b", re.I)),
    ("legal_context", re.compile(r"\b(?:attorney|lawyer|lawsuit|legal claim|court case|settlement|probation|criminal record)\b", re.I)),
    ("financial_context", re.compile(r"\b(?:bank account|routing number|credit report|debt collection|tax return|investment account)\b", re.I)),
    ("employment_context", re.compile(r"\b(?:resume|curriculum vitae|performance review|employee id|termination notice)\b", re.I)),
)


def sanitize_text(
    text: str,
    *,
    allow_sensitive: bool = False,
    override_reason: str | None = None,
) -> PrivacyDecision:
    """Redact identifiers and decide whether model processing is allowed."""

    if allow_sensitive and not (override_reason and override_reason.strip()):
        raise ValueError("A non-empty override_reason is required when allowing sensitive content")

    sanitized = text
    findings: list[PrivacyFinding] = []
    for kind, pattern, replacement in _REDACTIONS:
        sanitized, count = pattern.subn(replacement, sanitized)
        if count:
            findings.append(PrivacyFinding(kind, "redact", count))

    for kind, pattern in _HIGH_RISK_TERMS:
        count = len(pattern.findall(text))
        if count:
            findings.append(PrivacyFinding(kind, "review", count))

    secret_or_high_risk = any(item.action == "review" for item in findings) or any(
        item.kind in {"private_key", "api_key", "secret_assignment", "ssn", "credit_card"}
        for item in findings
    )
    blocked = secret_or_high_risk and not allow_sensitive
    return PrivacyDecision(
        text=sanitized,
        findings=tuple(findings),
        blocked=blocked,
        override_used=allow_sensitive,
        override_reason=override_reason.strip() if override_reason else None,
    )


def sanitize_for_model(text: str) -> PrivacyDecision:
    """Apply the default gate, with an explicit two-part operator override.

    The override is intentionally opt-in through both a boolean and a reason:
    ``ARTICLE_PIPELINE_ALLOW_SENSITIVE=true`` and
    ``ARTICLE_PIPELINE_PRIVACY_OVERRIDE_REASON=...``.
    """

    reason = os.environ.get("ARTICLE_PIPELINE_PRIVACY_OVERRIDE_REASON", "").strip()
    enabled = os.environ.get("ARTICLE_PIPELINE_ALLOW_SENSITIVE", "false").lower() in {"1", "true", "yes"}
    approved = enabled and bool(reason)
    return sanitize_text(text, allow_sensitive=approved, override_reason=reason if approved else None)


def findings_as_dicts(decision: PrivacyDecision) -> list[dict[str, object]]:
    """Return safe, non-content-bearing metadata for audit records."""

    return [dataclasses.asdict(item) for item in decision.findings]
