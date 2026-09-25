from privacy_filter import sanitize_for_model, sanitize_text


def test_identifiers_are_redacted_without_retaining_values() -> None:
    decision = sanitize_text("Email alice@example.test and call 415-555-0100.")
    assert "alice@example.test" not in decision.text
    assert "415-555-0100" not in decision.text
    assert "[REDACTED EMAIL]" in decision.text
    assert decision.blocked is False
    assert {item.kind for item in decision.findings} == {"email", "phone"}


def test_high_risk_content_blocks_by_default_and_requires_reason_for_override() -> None:
    decision = sanitize_text("My diagnosis is private and the api_key=sk-1234567890123456")
    assert decision.blocked is True
    assert "sk-1234567890123456" not in decision.text

    overridden = sanitize_text(
        "My diagnosis is private and the api_key=sk-1234567890123456",
        allow_sensitive=True,
        override_reason="Synthetic test of an approved editorial exception",
    )
    assert overridden.blocked is False
    assert overridden.override_used is True


def test_environment_override_requires_both_flag_and_reason(monkeypatch) -> None:
    value = "A private diagnosis should not reach a model by default."
    monkeypatch.setenv("ARTICLE_PIPELINE_ALLOW_SENSITIVE", "true")
    monkeypatch.delenv("ARTICLE_PIPELINE_PRIVACY_OVERRIDE_REASON", raising=False)
    assert sanitize_for_model(value).blocked is True
    monkeypatch.setenv("ARTICLE_PIPELINE_PRIVACY_OVERRIDE_REASON", "Editorial exception for synthetic review")
    decision = sanitize_for_model(value)
    assert decision.blocked is False
    assert decision.override_used is True
