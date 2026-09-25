from codex_provider import ProviderResult, ResilientProvider


def result(provider: str, model: str = "local-model") -> ProviderResult:
    return ProviderResult(
        text="{}",
        provider=provider,
        requested_model=model,
        actual_model=model,
        reasoning_effort="none",
        run_id=None,
        prompt_sha256="prompt",
        output_sha256="output",
    )


class Primary:
    model = "gpt-5.6-luna"

    def complete(self, prompt: str, **kwargs):
        raise RuntimeError("synthetic unavailable")


class Fallback:
    model = "local-model"

    def complete(self, prompt: str, **kwargs):
        return result("local-openai-compatible")


def test_fallback_records_requested_model_and_reason() -> None:
    value = ResilientProvider(Primary(), Fallback()).complete("safe prompt")
    assert value.provider == "local-openai-compatible"
    assert value.requested_model == "gpt-5.6-luna"
    assert "synthetic unavailable" in (value.fallback_reason or "")

