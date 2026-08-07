from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import public_interest_scorer as scorer


def complete_brief(**overrides: object) -> dict:
    value = {
        "topic": "Airflow debugging",
        "audience": "Python data engineers",
        "problem": "A DAG would not import",
        "resolution": "The final section fixed a bad import",
        "tools_and_technologies": ["Airflow", "Python"],
        "major_errors_or_false_starts": ["Changed the schedule first"],
        "reusable_lessons": ["Run the import error command"],
        "facts_and_code_to_preserve": ["airflow dags list-import-errors"],
        "article_angle": "Debug imports before runtime behavior",
        "uncertainties": [],
        "privacy_risks": [],
    }
    value.update(overrides)
    return value


def score_payload(value: float = 0.5, **overrides: object) -> dict:
    payload = {
        "public_interest_score": value,
        "technical_value_score": value,
        "article_potential_score": value,
        "salvageability_score": value,
        "topic": "Airflow debugging",
        "article_angle": "Fix imports first",
        "reasoning": "Reusable resolution",
        "privacy_risk": False,
        "material_uncertainty": False,
    }
    payload.update(overrides)
    return payload


class FakeScoringClient:
    provider = scorer.ProviderConfig("https://example.test/v1", "openrouter/free")

    def __init__(self, payload: dict):
        self.payload = payload

    def score(self, brief: dict, excerpts: str) -> tuple[dict, str, str]:
        assert brief["resolution"]
        assert excerpts
        return self.payload, "google/gemma-routed:free", "Google"


def test_weighted_score_and_exact_boundaries() -> None:
    assert scorer.weighted_score(score_payload(1.0)) == 1.0
    assert scorer.route_disposition(0.2999) == "reject"
    assert scorer.route_disposition(0.30) == "review"
    assert scorer.route_disposition(0.4999) == "review"
    assert scorer.route_disposition(0.50) == "rewrite"


def test_privacy_and_material_uncertainty_force_review() -> None:
    brief = complete_brief(privacy_risks=["Contains a private email address"])
    result = scorer.score_text(
        "---\nTitle: test\n---\nA useful resolved article.",
        brief=brief,
        client=FakeScoringClient(score_payload(0.9)),
    )
    assert result.disposition == "review"
    assert result.privacy_risk is True

    uncertain = scorer.score_text(
        "A useful article",
        brief=complete_brief(),
        client=FakeScoringClient(score_payload(0.9, material_uncertainty=True)),
    )
    assert uncertain.disposition == "review"


def test_low_evidence_source_is_capped_and_rejected() -> None:
    result = scorer.score_text(
        "---\nTitle: Empty\n---\nMinimal record with no useful details.",
        brief=complete_brief(),
        client=FakeScoringClient(score_payload(1.0)),
    )
    assert result.score < 0.30
    assert result.disposition == "reject"


def test_score_file_prefilters_low_evidence_without_a_model(tmp_path: Path) -> None:
    article = tmp_path / "short.md"
    article.write_text(
        "---\nTitle: Short query\nDate: 2026-08-05\nTags:\n  - Test\n---\nA tiny search query.\n",
        encoding="utf-8",
    )
    result, _ = scorer.score_file(article, source_dir=tmp_path, client=None)
    assert result.disposition == "reject"
    assert result.provider == "local-rules"
    assert scorer.frontmatter_value(article.read_text(encoding="utf-8"), "Score_Model") == "deterministic-prefilter"


def test_low_evidence_with_obvious_private_identifier_forces_review() -> None:
    result = scorer.deterministic_score("Contact private.person@example.com")
    assert result.privacy_risk is True
    assert result.disposition == "review"


def test_normalizes_observed_gemma_brief_wrapper() -> None:
    value = {
        "editorial_brief": {
            "article_title": "Safe archive extraction",
            "problem_to_resolution_arc": {"problem": "ZipSlip", "resolution": "Validate member paths"},
            "facts_and_code": {"libraries": ["zipfile"], "core_requirements": ["Reject traversal"]},
            "failed_attempts": [],
            "reusable_lessons": ["Resolve every member under the destination"],
            "uncertainty": "Exact policy is project-specific",
            "privacy_risks": [],
        }
    }
    brief = scorer.normalize_document_brief(value)
    assert brief["topic"] == "Safe archive extraction"
    assert brief["resolution"] == "Validate member paths"
    assert "zipfile" in brief["facts_and_code_to_preserve"]


def test_incomplete_brief_recovers_topic_from_source_title() -> None:
    brief = scorer.normalize_document_brief(
        {"resolution": "The final fix worked"},
        source_text="---\nTitle: Recovered article title\n---\nBody text",
    )
    assert brief["topic"] == "Recovered article title"
    assert brief["resolution"] == "The final fix worked"


def test_digest_merge_preserves_late_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scorer, "chunk_markdown", lambda text, base_url: ["early false start", "late final fix"])
    responses = [
        complete_brief(resolution="No resolution yet"),
        complete_brief(resolution="Late final fix solved the import"),
    ]

    def fake_chat(base_url, model, system, user, schema, **kwargs):
        if user.startswith("Merge these partial briefs"):
            assert "Late final fix solved the import" in user
            return complete_brief(resolution="Late final fix solved the import")
        return responses.pop(0)

    brief = scorer.build_document_brief(
        "long transcript",
        local_base_url="http://local/v1",
        model="/models/gemma.gguf",
        chat_json=fake_chat,
    )
    assert brief["resolution"] == "Late final fix solved the import"


def _response(status: int, payload: dict | None = None, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        headers=headers,
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
    )


def test_openrouter_retries_rate_limit_and_records_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [
        _response(429, {"error": "rate limited"}, {"Retry-After": "0"}),
        _response(
            200,
            {
                "model": "qwen/qwen3:free",
                "provider": "Together",
                "choices": [{"message": {"content": json.dumps(score_payload(0.7))}}],
            },
        ),
    ]
    monkeypatch.setattr(scorer.httpx, "post", lambda *args, **kwargs: responses.pop(0))
    monkeypatch.setattr(scorer.time, "sleep", lambda _: None)
    client = scorer.OpenRouterScoringClient(
        scorer.ProviderConfig("https://openrouter.ai/api/v1", "openrouter/free"), "secret", retries=2
    )
    raw, model, provider = client.score(complete_brief(), "excerpt")
    assert raw["technical_value_score"] == 0.7
    assert model == "qwen/qwen3:free"
    assert provider == "Together"


@pytest.mark.parametrize(
    "response",
    [
        _response(200, {"choices": [{"message": {"content": "not-json"}}]}),
        _response(503, {"error": "provider unavailable"}),
    ],
)
def test_openrouter_malformed_or_unavailable_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> None:
    monkeypatch.setattr(scorer.httpx, "post", lambda *args, **kwargs: response)
    client = scorer.OpenRouterScoringClient(
        scorer.ProviderConfig("https://openrouter.ai/api/v1", "openrouter/free"), "secret", retries=1
    )
    with pytest.raises(RuntimeError, match="OpenRouter scoring failed"):
        client.score(complete_brief(), "excerpt")


def test_score_file_persists_frontmatter_sidecar_and_manifest(tmp_path: Path) -> None:
    article = tmp_path / "article.md"
    article.write_text(
        "---\nTitle: Test\nDate: 2026-01-01\nTags:\n  - Test\n---\n"
        "A resolved Python and Airflow troubleshooting article with concrete diagnostics, reusable steps, "
        "and enough source evidence to support a practical explanation.\n\n"
        "```bash\nairflow dags list-import-errors\n```\n",
        encoding="utf-8",
    )
    result, record = scorer.score_file(
        article,
        source_dir=tmp_path,
        client=FakeScoringClient(score_payload(0.6)),
        brief=complete_brief(),
    )
    updated = article.read_text(encoding="utf-8")
    assert result.disposition == "rewrite"
    assert scorer.frontmatter_value(updated, "Score_Provider") == "Google"
    assert record.is_file()
    assert (tmp_path / "_article_pipeline" / "public_interest_scores.jsonl").is_file()
    assert list((tmp_path / "_article_pipeline" / "backups").rglob("*.pre-score.md"))
