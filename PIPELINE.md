# Google Takeout to article pipeline

This project processes manually acquired Google Takeout data. It does not
download Takeout data, automate Google account access, or inspect Codex OAuth
credential files.

## Runtime

- Host scripts use `openai-codex` and the local Codex app-server. Codex owns
  ChatGPT OAuth; the default request is `gpt-5.6-luna` with reasoning `max`.
- Docker Airflow uses `CODEX_RUNTIME=gateway` and a bearer-protected host
  gateway. The gateway delegates to the same provider and fallback policy.
- If Codex is unavailable, the configured local OpenAI-compatible server may be
  used automatically. The actual provider and failure reason are recorded.
- Every writing or rewriting prompt includes the exact required tone:
  `distinctly human-sounding educational professional in a casual setting`.
- Sensitive-content overrides require both
  `ARTICLE_PIPELINE_ALLOW_SENSITIVE=true` and a non-empty
  `ARTICLE_PIPELINE_PRIVACY_OVERRIDE_REASON`; overrides remain review-routed.

## Intake

Keep raw Takeout under `data/` and do not commit it. To plan a safe copy into
the ignored processing inbox:

```sh
python3 takeout_intake.py data inbox
```

Apply the copy only after inspecting the checksum plan:

```sh
python3 takeout_intake.py data inbox --apply
```

The command skips browser index pages and `.git` directories, never deletes
the source, and writes an intake checksum manifest. The Airflow DAG does not
auto-copy by default; set `TAKEOUT_AUTO_COPY=true` only when that handoff is
deliberate.

## Workflow

1. Discover archives or already extracted `MyActivity.json` records.
2. Validate archive members and extract without traversal or symlink hazards.
3. Normalize records into staging Markdown and classify with the controlled
   taxonomy.
4. Redact common identifiers and block high-risk content before model calls.
5. Deterministically prefilter low-evidence and duplicate records.
6. Clean transcript scaffolding and split only substantial topic divergences.
7. Build a document brief, score public interest, and route to rewrite, review,
   or reject.
8. Gather bounded research, rewrite protected Markdown, and retain backups.
9. Run taxonomy migration dry-run, content validation, and tests before an
   atomic category-slug publication.

The default model pilot selects `N` candidates (`ARTICLE_PIPELINE_PILOT_N`,
default 10). A manual DAG run may set `selection` to `N`, `HALF`, or `ALL` and
may set `pilot_n`/`max_model_articles`; the selected mode is recorded in the
run ledger.

## Airflow

```sh
docker compose -f airflow/compose.yaml up -d --build
docker compose -f airflow/compose.yaml exec airflow-scheduler airflow dags list-import-errors
```

Provision the host gateway token referenced by
`CODEX_GATEWAY_TOKEN_FILE_HOST` before starting the stack. The gateway should
bind only to the intended host interface and must remain bearer protected.
From the host, a typical launch is:

```sh
CODEX_RUNTIME=host .venv/bin/python codex_gateway.py \
  --host 127.0.0.1 --port 8787 \
  --token-file /Users/avery/.secrets/article-pipeline-codex-gateway-token
```

The UI is available at <http://localhost:8080>. Set the credentials in
`airflow/.env.example` before using the local stack beyond development.

## Required checks

Before publishing or moving content:

```sh
python3 migrate_taxonomy.py --source articles --dry-run
python3 validate_content.py articles
python3 -m pytest tests
```

The published tree must stay below the taxonomy hard limit, contain exactly one
allowlisted Category and Tags value per article, and keep the parent folder
equal to the category slug. Research uncertainty, privacy findings, and
time-sensitive claims require editorial review.

## Storage and recovery

Raw Takeout, extracted media, and full model artifacts belong in configured
external backup storage, not in the published tree. Staging manifests and
short-lived sidecars are retained locally for audit. See
`docs/BACKUP_POLICY.md`, `docs/architecture.md`, and the ADRs under
`docs/adr/` for the durable decisions.

Cleanup is dry-run first; an applied cleanup must provide an external restore
root, for example:

```sh
python3 backup_policy.py staging --dry-run
python3 backup_policy.py staging --apply --backup-root /Volumes/article-backup
```
