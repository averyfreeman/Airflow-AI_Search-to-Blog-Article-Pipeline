"""Shared model and prompt policy for every article-writing operation.

Keeping this policy in one small module makes the model choice, reasoning
effort, and required voice auditable across the standalone scripts and Airflow
workers.  It deliberately contains no credentials or user content.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


MODEL_ID = "gpt-5.6-luna"
REASONING_EFFORT = "max"
WRITING_TONE = "distinctly human-sounding educational professional in a casual setting"
TONE_INSTRUCTION = f'Tone: "{WRITING_TONE}".'


def with_tone(prompt: str) -> str:
    """Append the exact required voice instruction to a model prompt."""

    if TONE_INSTRUCTION in prompt:
        return prompt
    return f"{prompt.rstrip()}\n\n{TONE_INSTRUCTION}"


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

