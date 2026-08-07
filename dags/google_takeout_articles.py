"""Incremental Google Takeout activity-to-article pipeline for Airflow 3.3."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pendulum
from airflow.sdk import dag, get_current_context, task


PROJECT_ROOT = Path(os.environ.get("ARTICLE_PIPELINE_ROOT", "/workspace/content"))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))
INBOX = Path(os.environ.get("TAKEOUT_INBOX", str(PROJECT_ROOT / "inbox")))
RUNS = Path(os.environ.get("ARTICLE_PIPELINE_RUNS", str(PROJECT_ROOT / "staging")))
PUBLISHED = Path(os.environ.get("ARTICLE_PUBLISH_DIR", str(PROJECT_ROOT / "articles")))
LOCAL_BASE_URL = os.environ.get("LOCAL_LLM_BASE_URL", "http://host.docker.internal:11400/v1")
DRY_RUN = os.environ.get("ARTICLE_PIPELINE_DRY_RUN", "false").lower() in {"1", "true", "yes"}
MAX_MODEL_ARTICLES = max(1, int(os.environ.get("ARTICLE_PIPELINE_MAX_MODEL_ARTICLES", "100")))
STATE_ROOT = Path(os.environ.get("ARTICLE_PIPELINE_STATE", str(PROJECT_ROOT / "_article_pipeline_state")))
COMPLETED_RECORDS = STATE_ROOT / "completed_records.jsonl"


def _append_disposition(run_root: Path, event: dict[str, Any]) -> None:
    from activity_pipeline import append_jsonl, utc_now

    append_jsonl(
        run_root / "_article_pipeline" / "dispositions.jsonl",
        {"timestamp": utc_now(), **event},
    )


def _completed_state() -> tuple[set[str], set[str]]:
    record_ids: set[str] = set()
    content_hashes: set[str] = set()
    if not COMPLETED_RECORDS.exists():
        return record_ids, content_hashes
    for line in COMPLETED_RECORDS.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("source_record_id"):
            record_ids.add(str(item["source_record_id"]))
        if item.get("content_hash") and item.get("outcome") != "duplicate_prefilter":
            content_hashes.add(str(item["content_hash"]))
    return record_ids, content_hashes


def _mark_completed(article: Path, *, outcome: str, score: float, disposition: str) -> None:
    from activity_pipeline import append_jsonl, frontmatter_value, utc_now

    text = article.read_text(encoding="utf-8")
    append_jsonl(
        COMPLETED_RECORDS,
        {
            "timestamp": utc_now(),
            "source_record_id": frontmatter_value(text, "Source_Record_ID"),
            "content_hash": frontmatter_value(text, "Content_Hash"),
            "outcome": outcome,
            "score": score,
            "disposition": disposition,
        },
    )


def _model_batch_limit(conf: dict[str, Any] | None = None) -> int:
    """Return a run-specific batch size without exceeding the deployment safety cap."""
    configured = (conf or {}).get("max_model_articles", MAX_MODEL_ARTICLES)
    try:
        requested = int(configured)
    except (TypeError, ValueError):
        requested = MAX_MODEL_ARTICLES
    return max(1, min(requested, MAX_MODEL_ARTICLES))


@dag(
    dag_id="google_takeout_articles",
    schedule="0 3 * * *",
    start_date=pendulum.datetime(2026, 8, 5, tz="America/Los_Angeles"),
    catchup=False,
    max_active_runs=1,
    tags=["google-takeout", "articles", "local-llm"],
    default_args={"owner": "avery", "retries": 1, "retry_delay": timedelta(minutes=2)},
)
def google_takeout_articles() -> None:
    @task
    def initialize_run() -> str:
        context = get_current_context()
        run_id = str(context["run_id"])
        run_slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", run_id)
        root = RUNS / run_slug
        (root / "articles").mkdir(parents=True, exist_ok=True)
        return str(root)

    @task
    def discover_archives() -> list[str]:
        from activity_pipeline import discover_archives as discover

        INBOX.mkdir(parents=True, exist_ok=True)
        return [str(path) for path in discover(INBOX)]

    @task
    def validate_and_safely_extract(archive: str, run_root: str) -> dict[str, str]:
        from activity_pipeline import extract_archive, validate_archive

        validation = validate_archive(Path(archive))
        destination = Path(run_root) / "extracted" / validation.sha256[:16]
        extract_archive(validation, destination)
        return {
            "archive": str(validation.path),
            "archive_sha256": validation.sha256,
            "extracted": str(destination),
        }

    @task
    def normalize_activity_records(extraction: dict[str, str], run_root: str) -> list[str]:
        from activity_pipeline import process_input

        outputs = process_input(
            Path(extraction["extracted"]),
            Path(run_root) / "articles",
            local_base_url=LOCAL_BASE_URL,
        )
        return [str(path) for path in outputs]

    @task(pool="content_mutation", execution_timeout=timedelta(hours=1))
    def select_model_candidates(normalized: list[list[str]], run_root: str) -> list[str]:
        from activity_pipeline import append_jsonl, frontmatter_value
        from public_interest_scorer import requires_model_scoring, score_file

        article_dir = Path(run_root) / "articles"
        dag_run = get_current_context().get("dag_run")
        model_batch_limit = _model_batch_limit(getattr(dag_run, "conf", None))
        completed_ids, completed_hashes = _completed_state()
        pending: list[tuple[str, Path, str, str]] = []
        deterministic_count = 0
        duplicate_count = 0
        already_completed_count = 0

        for article_name in sorted({name for group in normalized for name in group}):
            article = Path(article_name)
            text = article.read_text(encoding="utf-8")
            record_id = frontmatter_value(text, "Source_Record_ID") or ""
            content_hash = frontmatter_value(text, "Content_Hash") or ""
            date = frontmatter_value(text, "Date") or ""
            if record_id and record_id in completed_ids:
                already_completed_count += 1
                continue
            if not requires_model_scoring(text):
                result, _ = score_file(article, source_dir=article_dir, client=None)
                _append_disposition(
                    Path(run_root),
                    {"event": "deterministic_prefilter", "article": str(article), "disposition": result.disposition},
                )
                _mark_completed(
                    article,
                    outcome="deterministic_prefilter",
                    score=result.score,
                    disposition=result.disposition,
                )
                if record_id:
                    completed_ids.add(record_id)
                if content_hash:
                    completed_hashes.add(content_hash)
                deterministic_count += 1
                continue
            pending.append((date, article, record_id, content_hash))

        pending.sort(key=lambda item: (item[0], item[1].name), reverse=True)
        unique_pending: list[Path] = []
        seen_hashes = set(completed_hashes)
        for _, article, record_id, content_hash in pending:
            if content_hash and content_hash in seen_hashes:
                result, _ = score_file(
                    article,
                    source_dir=article_dir,
                    client=None,
                    deterministic_reason="Duplicate content is already represented by another source record.",
                )
                _append_disposition(
                    Path(run_root),
                    {"event": "duplicate_prefilter", "article": str(article), "disposition": result.disposition},
                )
                _mark_completed(
                    article,
                    outcome="duplicate_prefilter",
                    score=result.score,
                    disposition=result.disposition,
                )
                if record_id:
                    completed_ids.add(record_id)
                duplicate_count += 1
                continue
            if content_hash:
                seen_hashes.add(content_hash)
            unique_pending.append(article)

        selected = unique_pending[:model_batch_limit]
        append_jsonl(
            Path(run_root) / "_article_pipeline" / "candidate_selection.jsonl",
            {
                "normalized": sum(len(group) for group in normalized),
                "already_completed": already_completed_count,
                "deterministic": deterministic_count,
                "duplicates": duplicate_count,
                "model_selected": len(selected),
                "deferred": max(0, len(unique_pending) - len(selected)),
                "model_batch_limit": model_batch_limit,
            },
        )
        return [str(path) for path in selected]

    @task(pool="local_llama", pool_slots=2, execution_timeout=timedelta(hours=4))
    def clean_and_split_articles(candidates: list[str], run_root: str) -> list[str]:
        article_dir = Path(run_root) / "articles"
        if not candidates:
            return []
        command = [
            sys.executable,
            str(PROJECT_ROOT / "content_article_surgeon.py"),
            "--source",
            str(article_dir),
            "--local-base-url",
            LOCAL_BASE_URL,
            "--local-model",
            "auto",
            "--split-evaluator",
            "local",
            "--conversation-auditor",
            "local",
            "--local-concurrency",
            "2",
            "--file-concurrency",
            "2",
        ]
        for article in candidates:
            command.extend(["--article", article])
        subprocess.run(
            command,
            check=True,
            cwd=PROJECT_ROOT,
        )
        selected = {Path(path).resolve() for path in candidates}
        selected_stems = {path.stem for path in selected}
        return [
            str(path)
            for path in sorted(article_dir.rglob("*.md"))
            if (
                "_article_pipeline" not in path.parts
                and path.name != "article_surgeon_progress_tree.md"
                and (path.resolve() in selected or any(path.stem.startswith(stem + "__split_") for stem in selected_stems))
            )
        ]

    @task(pool="local_llama", execution_timeout=timedelta(minutes=45))
    def build_global_brief(article: str) -> dict[str, Any]:
        from public_interest_scorer import build_document_brief

        text = Path(article).read_text(encoding="utf-8")
        return {
            "article": article,
            "brief": build_document_brief(text, local_base_url=LOCAL_BASE_URL),
        }

    @task(
        pool="openrouter",
        retries=4,
        retry_delay=timedelta(seconds=30),
        retry_exponential_backoff=True,
        max_retry_delay=timedelta(minutes=10),
        execution_timeout=timedelta(minutes=30),
    )
    def score_public_interest(item: dict[str, Any], run_root: str) -> dict[str, Any]:
        from public_interest_scorer import (
            OpenRouterScoringClient,
            load_api_key,
            load_provider_config,
            score_file,
        )

        provider = load_provider_config(PROJECT_ROOT / "opencode.json")
        client = OpenRouterScoringClient(provider, load_api_key())
        result, record = score_file(
            Path(item["article"]),
            source_dir=Path(run_root) / "articles",
            client=client,
            local_base_url=LOCAL_BASE_URL,
            brief=item["brief"],
        )
        return {
            "article": item["article"],
            "score": result.score,
            "disposition": result.disposition,
            "score_record": str(record),
        }

    @task.branch
    def choose_article_paths(scored: list[dict[str, Any]]) -> list[str] | None:
        dispositions = {item["disposition"] for item in scored}
        selected = [f"{name}_article" for name in ("rewrite", "review", "reject") if name in dispositions]
        return selected or None

    @task(pool="local_llama", execution_timeout=timedelta(hours=2))
    def rewrite_article(item: dict[str, Any], run_root: str) -> dict[str, Any]:
        from airflow.sdk.exceptions import AirflowSkipException
        from article_rewriter import LocalRewriteClient, rewrite_file

        if item["disposition"] != "rewrite":
            raise AirflowSkipException("Article is not routed to rewrite")
        result = rewrite_file(
            Path(item["article"]),
            source_dir=Path(run_root) / "articles",
            client=LocalRewriteClient(LOCAL_BASE_URL),
            research=True,
        )
        return {**item, **result}

    @task(pool="content_mutation")
    def review_article(item: dict[str, Any], run_root: str) -> dict[str, Any]:
        from airflow.sdk.exceptions import AirflowSkipException

        if item["disposition"] != "review":
            raise AirflowSkipException("Article is not routed to editorial review")
        _append_disposition(Path(run_root), {"event": "editorial_review", **item})
        _mark_completed(
            Path(item["article"]), outcome="editorial_review", score=float(item["score"]), disposition="review"
        )
        return item

    @task(pool="content_mutation")
    def reject_article(item: dict[str, Any], run_root: str) -> dict[str, Any]:
        from airflow.sdk.exceptions import AirflowSkipException

        if item["disposition"] != "reject":
            raise AirflowSkipException("Article is not routed to reject")
        _append_disposition(Path(run_root), {"event": "rejected", **item})
        _mark_completed(Path(item["article"]), outcome="rejected", score=float(item["score"]), disposition="reject")
        return item

    @task
    def validate_output(item: dict[str, Any]) -> dict[str, Any]:
        from activity_pipeline import frontmatter_value, split_frontmatter
        from article_rewriter import TRANSCRIPT_MARKER_RE

        article = Path(item["article"])
        text = article.read_text(encoding="utf-8")
        frontmatter, body = split_frontmatter(text)
        required = ["Title", "Date", "Category", "Score_Disposition", "Rewritten_At"]
        missing = [key for key in required if frontmatter_value(text, key) in {None, ""}]
        if not re.search(r"(?m)^Tags:\s*\n[ \t]+-\s+\S+\s*$", frontmatter):
            missing.append("Tags")
        from taxonomy import category
        try:
            canonical = category(frontmatter_value(text, "Category") or "")
        except ValueError:
            missing.append("valid Category")
        else:
            if not re.search(rf"(?m)^\s*-\s+{re.escape(canonical.label)}\s*$", frontmatter):
                missing.append("Tags matching Category")
        if missing:
            raise ValueError(f"{article} is missing required front matter: {missing}")
        if len(body.strip()) < 100:
            raise ValueError(f"{article} is too short to publish")
        if TRANSCRIPT_MARKER_RE.search(body):
            raise ValueError(f"{article} still contains transcript markers")
        return item

    @task(pool="content_mutation")
    def atomically_publish(item: dict[str, Any], run_root: str) -> str:
        from activity_pipeline import atomic_write

        source = Path(item["article"])
        from activity_pipeline import frontmatter_value
        from taxonomy import category

        canonical = category(frontmatter_value(source.read_text(encoding="utf-8"), "Category") or "")
        destination = PUBLISHED / canonical.slug / source.name
        if DRY_RUN:
            _append_disposition(
                Path(run_root),
                {"event": "publish_dry_run", "source": str(source), "destination": str(destination)},
            )
            return f"dry-run:{destination}"
        content = source.read_text(encoding="utf-8")
        if destination.exists():
            existing = destination.read_text(encoding="utf-8")
            if existing == content:
                _mark_completed(source, outcome="published", score=float(item["score"]), disposition="rewrite")
                return str(destination)
            backup = Path(run_root) / "_article_pipeline" / "published_backups" / destination.name
            atomic_write(backup, existing)
        atomic_write(destination, content)
        _append_disposition(
            Path(run_root),
            {"event": "published", "source": str(source), "destination": str(destination)},
        )
        _mark_completed(source, outcome="published", score=float(item["score"]), disposition="rewrite")
        return str(destination)

    run_root = initialize_run()
    archives = discover_archives()
    extracted = validate_and_safely_extract.partial(run_root=run_root).expand(archive=archives)
    normalized = normalize_activity_records.partial(run_root=run_root).expand(extraction=extracted)
    candidates = select_model_candidates(normalized, run_root)
    cleaned = clean_and_split_articles(candidates, run_root)
    briefs = build_global_brief.expand(article=cleaned)
    scored = score_public_interest.partial(run_root=run_root).expand(item=briefs)
    branch = choose_article_paths(scored)

    rewritten = rewrite_article.partial(run_root=run_root).expand(item=scored)
    reviewed = review_article.partial(run_root=run_root).expand(item=scored)
    rejected = reject_article.partial(run_root=run_root).expand(item=scored)
    branch >> [rewritten, reviewed, rejected]

    validated = validate_output.expand(item=rewritten)
    atomically_publish.partial(run_root=run_root).expand(item=validated)


google_takeout_articles()
