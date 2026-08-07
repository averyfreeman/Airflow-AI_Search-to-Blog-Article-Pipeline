#!/usr/bin/env python3
"""Digest and score Markdown articles for public-interest/article potential.

Long inputs are summarized into a single global document brief before the
OpenRouter classifier assigns rubric dimensions. Chunk scores are never
averaged; Python computes the final weighted score deterministically.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

from activity_pipeline import (
    append_jsonl,
    atomic_write,
    discover_single_model,
    frontmatter_value,
    split_frontmatter,
    strip_thought_channels,
    update_frontmatter,
)


RUBRIC_VERSION = "public-interest-v1"
REWRITE_THRESHOLD = 0.50
REVIEW_THRESHOLD = 0.30
# Keep prompt evaluation comfortably below one 65,792-token llama.cpp slot. Smaller
# digest chunks are materially faster on local Apple Silicon and leave room for the
# schema, instructions, partial-brief merge, and generated JSON.
DIGEST_CHUNK_TOKENS = 20_000
DEFAULT_LOCAL_BASE_URL = os.environ.get("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11400/v1")
DEFAULT_CONFIG_PATH = Path(__file__).with_name("opencode.json")
DEFAULT_KEY_FILE = Path("/Users/avery/.secrets/Macclawd/openrouter-api-key")

SCORE_WEIGHTS = {
    "public_interest_score": 0.30,
    "technical_value_score": 0.25,
    "article_potential_score": 0.25,
    "salvageability_score": 0.20,
}

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
        "audience": {"type": "string"},
        "problem": {"type": "string"},
        "resolution": {"type": "string"},
        "tools_and_technologies": {"type": "array", "items": {"type": "string"}},
        "major_errors_or_false_starts": {"type": "array", "items": {"type": "string"}},
        "reusable_lessons": {"type": "array", "items": {"type": "string"}},
        "facts_and_code_to_preserve": {"type": "array", "items": {"type": "string"}},
        "article_angle": {"type": "string"},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
        "privacy_risks": {"type": "array", "items": {"type": "string"}},
        "research_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "topic",
        "audience",
        "problem",
        "resolution",
        "tools_and_technologies",
        "major_errors_or_false_starts",
        "reusable_lessons",
        "facts_and_code_to_preserve",
        "article_angle",
        "uncertainties",
        "privacy_risks",
        "research_questions",
    ],
    "additionalProperties": False,
}

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "public_interest_score": {"type": "number", "minimum": 0, "maximum": 1},
        "technical_value_score": {"type": "number", "minimum": 0, "maximum": 1},
        "article_potential_score": {"type": "number", "minimum": 0, "maximum": 1},
        "salvageability_score": {"type": "number", "minimum": 0, "maximum": 1},
        "topic": {"type": "string"},
        "article_angle": {"type": "string"},
        "reasoning": {"type": "string"},
        "privacy_risk": {"type": "boolean"},
        "material_uncertainty": {"type": "boolean"},
    },
    "required": [*SCORE_WEIGHTS, "topic", "article_angle", "reasoning", "privacy_risk", "material_uncertainty"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    model: str


@dataclass(frozen=True)
class ScoreResult:
    score: float
    disposition: str
    dimensions: dict[str, float]
    brief: dict[str, Any]
    topic: str
    article_angle: str
    reasoning: str
    privacy_risk: bool
    material_uncertainty: bool
    requested_model: str
    actual_model: str
    provider: str
    scored_at: str


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def extract_json_object(raw: str) -> dict[str, Any]:
    cleaned = strip_thought_channels(raw).strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    decoded: list[dict[str, Any]] = []
    for match in re.finditer(r"\{", cleaned):
        try:
            value, _ = decoder.raw_decode(cleaned[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            decoded.append(value)
    if decoded:
        return decoded[-1]
    raise ValueError(f"No JSON object found in model response: {cleaned[:300]!r}")


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, dict):
        result: list[str] = []
        for nested in value.values():
            result.extend(_string_list(nested))
        return result
    if value is None or value == "":
        return []
    return [str(value).strip()]


def normalize_document_brief(value: dict[str, Any], *, source_text: str = "") -> dict[str, Any]:
    """Normalize known Gemma wrapper variants into the canonical brief schema."""

    if all(key in value for key in BRIEF_SCHEMA["required"]):
        return {
            key: (_string_list(value[key]) if BRIEF_SCHEMA["properties"][key]["type"] == "array" else str(value[key]))
            for key in BRIEF_SCHEMA["required"]
        }

    nested = value.get("editorial_brief") if isinstance(value.get("editorial_brief"), dict) else value
    arc = nested.get("problem_to_resolution_arc", {})
    if not isinstance(arc, dict):
        arc = {}
    facts = nested.get("facts_and_code", nested.get("facts_and_code_to_preserve", []))
    title = str(nested.get("article_title", nested.get("topic", "Untitled activity"))).strip()
    source_title = frontmatter_value(source_text, "Title") if source_text else None
    _, source_body = split_frontmatter(source_text) if source_text else ("", "")
    source_excerpt = re.sub(r"\s+", " ", source_body).strip()[:500]
    normalized = {
        "topic": title if title != "Untitled activity" else (source_title or "Untitled activity"),
        "audience": str(nested.get("audience", "Readers interested in the topic")).strip(),
        "problem": str(nested.get("problem", arc.get("problem", ""))).strip()
        or (f"Source material: {source_excerpt}" if source_excerpt else "Problem not stated in the source."),
        "resolution": str(nested.get("resolution", arc.get("resolution", ""))).strip()
        or "No explicit resolution was identified in the source.",
        "tools_and_technologies": _string_list(
            nested.get("tools_and_technologies", facts.get("libraries", []) if isinstance(facts, dict) else [])
        ),
        "major_errors_or_false_starts": _string_list(
            nested.get("major_errors_or_false_starts", nested.get("failed_attempts", []))
        ),
        "reusable_lessons": _string_list(
            nested.get("reusable_lessons", nested.get("lessons_learned", []))
        ),
        "facts_and_code_to_preserve": _string_list(facts),
        "article_angle": str(nested.get("article_angle", title)).strip(),
        "uncertainties": _string_list(nested.get("uncertainties", nested.get("uncertainty", []))),
        "privacy_risks": _string_list(nested.get("privacy_risks", [])),
        "research_questions": _string_list(nested.get("research_questions", nested.get("questions_to_verify", []))),
    }
    if normalized["resolution"].startswith("No explicit resolution"):
        normalized["uncertainties"].append(
            "No explicit resolution was identified; editorial verification is required."
        )
    return normalized


def load_provider_config(path: Path = DEFAULT_CONFIG_PATH) -> ProviderConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    provider = raw.get("provider", {}).get("openrouter", {})
    base_url = str(provider.get("options", {}).get("baseURL", "https://openrouter.ai/api/v1")).rstrip("/")
    configured = str(raw.get("agent", {}).get("planner", {}).get("model", "openrouter:openrouter/free"))
    model = configured.split(":", 1)[1] if configured.startswith("openrouter:") else configured
    available = provider.get("models", {})
    if available and model not in available:
        raise ValueError(f"Configured scoring model {model!r} is not declared under provider.openrouter.models")
    return ProviderConfig(base_url=base_url, model=model)


def load_api_key(key_file: Path | None = None) -> str:
    environment = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if environment:
        return environment
    configured_path = os.environ.get("OPENROUTER_API_KEY_FILE", "").strip()
    path = Path(configured_path) if configured_path else (key_file or DEFAULT_KEY_FILE)
    if not path.is_file():
        raise RuntimeError(f"OpenRouter key file was not found: {path}")
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError(f"OpenRouter key file is empty: {path}")
    return key


def clamp_score(value: Any) -> float:
    return max(0.0, min(1.0, float(value)))


def weighted_score(dimensions: dict[str, Any]) -> float:
    return round(sum(clamp_score(dimensions[name]) * weight for name, weight in SCORE_WEIGHTS.items()), 4)


def route_disposition(score: float, *, privacy_risk: bool = False, material_uncertainty: bool = False) -> str:
    if privacy_risk or material_uncertainty:
        return "review"
    if score >= REWRITE_THRESHOLD:
        return "rewrite"
    if score >= REVIEW_THRESHOLD:
        return "review"
    return "reject"


def token_count(base_url: str, text: str, timeout: float = 30.0) -> int:
    try:
        response = httpx.post(
            base_url.removesuffix("/v1").rstrip("/") + "/tokenize",
            json={"content": text, "add_special": False},
            timeout=timeout,
        )
        response.raise_for_status()
        return len(response.json().get("tokens", []))
    except Exception:
        return max(1, len(text) // 4)


def chunk_markdown(text: str, base_url: str, max_tokens: int = DIGEST_CHUNK_TOKENS) -> list[str]:
    if token_count(base_url, text) <= max_tokens:
        return [text]
    sections = re.split(r"(?=^#{1,6}\s+)", text, flags=re.M)
    chunks: list[str] = []
    current = ""
    for section in sections:
        candidate = current + section
        if current and token_count(base_url, candidate) > max_tokens:
            chunks.append(current.strip())
            current = section
        else:
            current = candidate
        while current and token_count(base_url, current) > max_tokens:
            approximate_chars = max_tokens * 3
            boundary = current.rfind("\n\n", 0, approximate_chars)
            if boundary < approximate_chars // 2:
                boundary = approximate_chars
            chunks.append(current[:boundary].strip())
            current = current[boundary:].lstrip()
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _chat_json(
    base_url: str,
    model: str,
    system: str,
    user: str,
    schema: dict[str, Any],
    *,
    timeout: float,
    max_tokens: int,
) -> dict[str, Any]:
    response = httpx.post(
        base_url.rstrip("/") + "/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "document_brief", "strict": True, "schema": schema},
            },
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return extract_json_object(response.json()["choices"][0]["message"]["content"])


def build_document_brief(
    text: str,
    *,
    local_base_url: str = DEFAULT_LOCAL_BASE_URL,
    timeout: float = 240.0,
    model: str | None = None,
    chat_json: Callable[..., dict[str, Any]] = _chat_json,
) -> dict[str, Any]:
    local_model = model or discover_single_model(local_base_url)
    _, body = split_frontmatter(text)
    chunks = chunk_markdown(body, local_base_url)
    system = (
        "Create a factual editorial brief for a conversation-derived article. Do not score it. "
        "Capture the complete problem-to-resolution arc, including useful failed attempts, reusable lessons, "
        "facts/code worth preserving, missing context that external research should verify, uncertainty, and privacy risks. "
        "Separate source-supported facts from hypotheses and write 2-4 concrete research questions for an editor. Privacy risk means private source data such "
        "as personal identifiers, secrets, or sensitive circumstances, not a general cybersecurity topic. "
        "Use exactly the required top-level schema keys and never wrap the object. Return only schema-valid JSON."
    )
    partials: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        partials.append(
            normalize_document_brief(chat_json(
                local_base_url,
                local_model,
                system,
                f"Document section {index} of {len(chunks)}:\n\n{chunk}",
                BRIEF_SCHEMA,
                timeout=timeout,
                max_tokens=1200,
            ), source_text=text)
        )
    if len(partials) == 1:
        return partials[0]
    return normalize_document_brief(
        chat_json(
            local_base_url,
            local_model,
            system,
            "Merge these partial briefs into one coherent document-level brief. Do not omit a late resolution "
            "because earlier sections contain false starts.\n\n" + json.dumps(partials, ensure_ascii=False, indent=2),
            BRIEF_SCHEMA,
            timeout=timeout,
            max_tokens=1600,
        ),
        source_text=text,
    )


def representative_excerpts(text: str, max_chars: int = 18_000) -> str:
    _, body = split_frontmatter(text)
    if len(body) <= max_chars:
        return body
    part = max_chars // 3
    middle_start = max(0, len(body) // 2 - part // 2)
    return (
        body[:part]
        + "\n\n[... middle excerpt ...]\n\n"
        + body[middle_start : middle_start + part]
        + "\n\n[... ending excerpt ...]\n\n"
        + body[-part:]
    )


class OpenRouterScoringClient:
    def __init__(self, provider: ProviderConfig, api_key: str, timeout: float = 240.0, retries: int = 3):
        self.provider = provider
        self.api_key = api_key
        self.timeout = timeout
        self.retries = retries

    def score(self, brief: dict[str, Any], excerpts: str) -> tuple[dict[str, Any], str, str]:
        system = (
            "You are a conservative editorial opportunity classifier. Score the article concept, not its current "
            "chatty formatting. A messy troubleshooting transcript can have high salvageability and article potential "
            "when it contains a real resolution and reusable lessons. Scores above 0.8 require exceptional, source-"
            "supported depth; 0.5 means useful but ordinary; 0 means no recoverable public value. Privacy risk means "
            "private source data, not discussion of cybersecurity. Return only schema-valid JSON."
        )
        user = (
            "Use these dimensions independently:\n"
            "- public_interest_score: relevance to readers beyond the original author\n"
            "- technical_value_score: concrete, accurate, reusable practical depth\n"
            "- article_potential_score: strength of the angle and problem-to-resolution arc\n"
            "- salvageability_score: value recoverable through editing despite transcript messiness\n\n"
            f"Global document brief:\n{json.dumps(brief, ensure_ascii=False, indent=2)}\n\n"
            f"Representative source excerpts:\n{excerpts}"
        )
        payload = {
            "model": self.provider.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "public_interest_score", "strict": True, "schema": SCORE_SCHEMA},
            },
            "provider": {"require_parameters": True},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost/google-takeout-article-pipeline",
            "X-Title": "Google Takeout Article Pipeline",
        }
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = httpx.post(
                    self.provider.base_url + "/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )
                if response.status_code in {429, 503} and attempt < self.retries:
                    delay = float(response.headers.get("Retry-After", attempt * 2))
                    time.sleep(min(delay, 60.0))
                    continue
                response.raise_for_status()
                data = response.json()
                parsed = extract_json_object(data["choices"][0]["message"]["content"])
                return parsed, str(data.get("model", self.provider.model)), str(data.get("provider", "openrouter"))
            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(attempt * 2)
        raise RuntimeError(f"OpenRouter scoring failed after {self.retries} attempts: {last_error}")


def score_text(
    text: str,
    *,
    brief: dict[str, Any],
    client: OpenRouterScoringClient,
) -> ScoreResult:
    raw, actual_model, provider = client.score(brief, representative_excerpts(text))
    dimensions = {name: clamp_score(raw[name]) for name in SCORE_WEIGHTS}
    _, body = split_frontmatter(text)
    evidence_chars = len(re.sub(r"\s+", " ", body).strip())
    has_concrete_artifact = bool(re.search(r"```|https?://|\b(?:python|curl|airflow|docker|git)\b", body, re.I))
    if evidence_chars < 160 and not has_concrete_artifact:
        caps = {
            "public_interest_score": 0.15,
            "technical_value_score": 0.10,
            "article_potential_score": 0.10,
            "salvageability_score": 0.20,
        }
        dimensions = {name: min(value, caps[name]) for name, value in dimensions.items()}
    score = weighted_score(dimensions)
    privacy_risk = bool(raw.get("privacy_risk")) or bool(brief.get("privacy_risks"))
    material_uncertainty = bool(raw.get("material_uncertainty")) or any(
        "editorial verification is required" in str(item).lower()
        for item in brief.get("uncertainties", [])
    )
    return ScoreResult(
        score=score,
        disposition=route_disposition(score, privacy_risk=privacy_risk, material_uncertainty=material_uncertainty),
        dimensions=dimensions,
        brief=brief,
        topic=str(raw.get("topic", brief.get("topic", ""))).strip(),
        article_angle=str(raw.get("article_angle", brief.get("article_angle", ""))).strip(),
        reasoning=str(raw.get("reasoning", "")).strip(),
        privacy_risk=privacy_risk,
        material_uncertainty=material_uncertainty,
        requested_model=client.provider.model,
        actual_model=actual_model,
        provider=provider,
        scored_at=utc_now(),
    )


def source_evidence(text: str) -> tuple[int, bool]:
    """Return normalized body length and whether it contains a concrete artifact."""

    _, body = split_frontmatter(text)
    evidence_chars = len(re.sub(r"\s+", " ", body).strip())
    has_concrete_artifact = bool(
        re.search(r"```|https?://|\b(?:python|curl|airflow|docker|git|terraform|sql)\b", body, re.I)
    )
    return evidence_chars, has_concrete_artifact


def requires_model_scoring(text: str) -> bool:
    evidence_chars, has_concrete_artifact = source_evidence(text)
    return evidence_chars >= 160 or has_concrete_artifact


def obvious_privacy_risk(text: str) -> bool:
    """Conservatively identify common private identifiers without an external call."""

    patterns = (
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b",
        r"\b\d{3}-\d{2}-\d{4}\b",
        r"\b(?:sk-[A-Za-z0-9_-]{16,}|api[_ -]?key\s*[:=])",
    )
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def deterministic_score(
    text: str,
    *,
    reason: str = "Insufficient source evidence for model scoring.",
) -> ScoreResult:
    brief = normalize_document_brief({}, source_text=text)
    brief["uncertainties"] = [reason]
    dimensions = {
        "public_interest_score": 0.12,
        "technical_value_score": 0.08,
        "article_potential_score": 0.10,
        "salvageability_score": 0.18,
    }
    score = weighted_score(dimensions)
    privacy_risk = obvious_privacy_risk(text)
    return ScoreResult(
        score=score,
        disposition=route_disposition(score, privacy_risk=privacy_risk),
        dimensions=dimensions,
        brief=brief,
        topic=brief["topic"],
        article_angle=brief["article_angle"],
        reasoning=reason,
        privacy_risk=privacy_risk,
        material_uncertainty=False,
        requested_model="deterministic-prefilter",
        actual_model="deterministic-prefilter",
        provider="local-rules",
        scored_at=utc_now(),
    )


def score_record_path(article: Path, source_dir: Path) -> Path:
    digest = hashlib.sha256(str(article.resolve()).encode("utf-8")).hexdigest()[:20]
    return source_dir / "_article_pipeline" / "scores" / f"{digest}.score.json"


def append_manifest(source_dir: Path, record: dict[str, Any]) -> None:
    path = source_dir / "_article_pipeline" / "public_interest_scores.jsonl"
    append_jsonl(path, record)


def backup_before_scoring(article: Path, source_dir: Path, text: str) -> Path:
    relative = article.resolve().relative_to(source_dir.resolve())
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    target = (
        source_dir
        / "_article_pipeline"
        / "backups"
        / relative.parent
        / f"{article.stem}.{digest}.pre-score.md"
    )
    if not target.exists():
        atomic_write(target, text)
    return target


def score_file(
    article: Path,
    *,
    source_dir: Path,
    client: OpenRouterScoringClient | None,
    local_base_url: str = DEFAULT_LOCAL_BASE_URL,
    dry_run: bool = False,
    brief: dict[str, Any] | None = None,
    brief_builder: Callable[..., dict[str, Any]] = build_document_brief,
    deterministic_reason: str | None = None,
) -> tuple[ScoreResult, Path]:
    text = article.read_text(encoding="utf-8")
    if deterministic_reason or not requires_model_scoring(text):
        result = deterministic_score(text, reason=deterministic_reason or "Insufficient source evidence for model scoring.")
    else:
        if client is None:
            raise ValueError("An OpenRouter client is required for model-scored content")
        brief = brief or brief_builder(text, local_base_url=local_base_url)
        result = score_text(text, brief=brief, client=client)
    record_path = score_record_path(article, source_dir)
    backup_path = source_dir / "_article_pipeline" / "backups" / "dry-run"
    record = {
        "article": str(article.resolve()),
        "source_record_id": frontmatter_value(text, "Source_Record_ID"),
        **result.__dict__,
    }
    if not dry_run:
        backup_path = backup_before_scoring(article, source_dir, text)
        record["backup"] = str(backup_path)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(record_path, json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        updated = update_frontmatter(
            text,
            {
                "Public_Interest_Score": result.score,
                "Score_Disposition": result.disposition,
                "Score_Version": RUBRIC_VERSION,
                "Score_Model": result.actual_model,
                "Score_Provider": result.provider,
                "Article_Angle": result.article_angle,
                "Topic": result.topic,
                "Scored_At": result.scored_at,
                "Score_Record": str(record_path.relative_to(source_dir)),
            },
        )
        atomic_write(article, updated)
        append_manifest(source_dir, record)
    return result, record_path


def iter_markdown(source: Path, recursive: bool = True) -> Iterable[Path]:
    iterator = source.rglob("*.md") if recursive else source.glob("*.md")
    for path in sorted(iterator):
        if "_article_pipeline" not in path.parts and "backup" not in path.parts:
            yield path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("./staging"))
    parser.add_argument("--article", type=Path, action="append")
    parser.add_argument("--opencode-config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--local-base-url", default=DEFAULT_LOCAL_BASE_URL)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-recursive", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    provider = load_provider_config(args.opencode_config)
    client = OpenRouterScoringClient(provider, load_api_key(args.key_file))
    articles = args.article or list(iter_markdown(args.source, recursive=not args.no_recursive))
    summaries: list[dict[str, Any]] = []
    for article in articles:
        result, record_path = score_file(
            article,
            source_dir=args.source,
            client=client,
            local_base_url=args.local_base_url,
            dry_run=args.dry_run,
        )
        summaries.append({"article": str(article), "score": result.score, "disposition": result.disposition, "record": str(record_path)})
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
