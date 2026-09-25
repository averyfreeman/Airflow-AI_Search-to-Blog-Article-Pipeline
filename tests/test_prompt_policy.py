from prompt_policy import MODEL_ID, REASONING_EFFORT, TONE_INSTRUCTION, with_tone


def test_model_policy_and_exact_tone_are_shared() -> None:
    prompt = with_tone("Rewrite this article.")
    assert MODEL_ID == "gpt-5.6-luna"
    assert REASONING_EFFORT == "max"
    assert TONE_INSTRUCTION in prompt
    assert with_tone(prompt) == prompt

