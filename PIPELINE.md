# Google Takeout to article pipeline

The `google_takeout_articles` DAG runs daily at 03:00 America/Los_Angeles and can also be triggered manually. It processes manually downloaded Google Takeout archives from `inbox/`; authenticated Takeout downloading is intentionally deferred to a later version. The optional `takeout_downloader_script` submodule is not imported or invoked by the Airflow DAG; it can be run separately to acquire archives for `inbox/`.

The published article tree is intentionally kept out of this public repository.
Publishing runs must make the private `UnixGreybeard.Org-Articles` repository
available as the local `articles/` directory, or set `ARTICLE_PUBLISH_DIR` to a
private checkout of that repository.

## Runtime

- Airflow 3.3 with LocalExecutor and Postgres runs from `airflow/compose.yaml`.
- Local Gemma is reached from containers at `http://host.docker.internal:11400/v1`.
- The model ID is discovered from `/v1/models` and sent exactly as returned. Logs use only the GGUF basename.
- llama.cpp currently exposes two slots with 65,792 tokens per request; Airflow's `local_llama` pool therefore has two slots.
- OpenRouter scoring uses `openrouter/free`, with two Airflow pool slots and task retry/backoff.
- Publishing is serialized through the one-slot `content_mutation` pool.
- At most 100 previously unprocessed, unique, evidence-bearing records enter model processing per daily run. Change `ARTICLE_PIPELINE_MAX_MODEL_ARTICLES` to tune this cap. Manual runs may request a smaller pilot with DAG-run configuration such as `{"max_model_articles": 10}`; run configuration cannot exceed the deployment cap.

## Start and operate Airflow

```bash
cd /path/to/Airflow-AI_Search-to-Blog-Article-Pipeline
docker compose -f airflow/compose.yaml up -d --build
docker compose -f airflow/compose.yaml exec airflow-scheduler airflow dags list-import-errors
```

The local UI is available at <http://localhost:8080>. Development credentials default to `airflow` / `airflow`; set the values shown in `airflow/.env.example` before using this beyond the local machine.

Place one or more `.zip`, `.tar`, `.tar.gz`, or `.tgz` exports in `inbox/`. Trigger `google_takeout_articles` in the UI or wait for its schedule. Set `ARTICLE_PIPELINE_DRY_RUN=1` in the common Compose environment when validating a run that must not publish.

To stop the stack without deleting its Postgres volume:

```bash
docker compose -f airflow/compose.yaml stop
```

## Data flow and outputs

The DAG safely extracts archives, normalizes AI Mode and Gemini Apps records, assigns one allowlisted broad category, deterministically rejects low-evidence and duplicate records, cleans and conservatively splits a bounded candidate batch, builds one document-level brief, scores it, gathers a bounded external evidence packet for rewrites, and branches each article to rewrite, review, or reject. Only validated rewrites are atomically published to the matching category folder under `articles/`. Completed source-record and representative-content hashes are retained in `_article_pipeline_state/completed_records.jsonl`, so overlapping Takeout exports advance incrementally instead of rescoring the same history.

Run artifacts are kept below `staging/<run-id>/_article_pipeline/`. The scorer records dimensions, weighted score, disposition, rubric version, requested and actual routed models, provider, timestamp, privacy/uncertainty flags, and the global brief. It does not move source articles into score directories.

## Content quality and taxonomy checks

Run the deterministic checks before publishing or reviewing a migration:

```sh
.venv/bin/python migrate_taxonomy.py --source articles --dry-run
.venv/bin/python validate_content.py articles
.venv/bin/python -m pytest -q tests
```

Rewrites from the DAG use external research by passing `research=True` to the
rewriter. The research packet is bounded, records source URLs and extraction
limits, and treats search results as discovery aids rather than evidence by
themselves. Set `ARTICLE_RESEARCH_SEARCH_URL` when deploying an approved
search provider instead of the default HTML search adapter.

Raw archives, extracted media, and long-lived rewrite backups follow
`docs/BACKUP_POLICY.md`; they should not accumulate under the publication tree.

## Secrets

`opencode.json` contains only the file reference `{file:~/.secrets/Macclawd/openrouter-api-key}`. Compose mounts that owner-only file at `/run/secrets/openrouter_api_key`; the key must never be passed through task arguments, XCom, logs, or manifests.

The OpenRouter key that previously appeared literally in project configuration should be rotated in OpenRouter. This pipeline does not alter credentials outside this project.

## Verification

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q
docker compose -f airflow/compose.yaml config --quiet
docker compose -f airflow/compose.yaml exec airflow-scheduler airflow dags list-import-errors --output json
```

The anonymized schema/archive fixtures are under `tests/`. The retained synthetic end-to-end run evidence is archived under `backup/2026-08-05_pre_pipeline_refactor/dry_runs/`.
