# Article pipeline backup policy

## What stays here

Keep published Markdown, Python source, tests, small JSONL state/manifests, and
the current short-lived staging window. Do not keep raw Takeout exports,
extracted attachments, PDFs, images, audio, or full model transcripts in the
publication tree.

The working `data/` tree and ignored `inbox/` are source-processing locations,
not publication inputs to commit. `takeout_intake.py` records SHA-256 values
when files are staged. The host-side Codex gateway must not log prompts or
model output; retain only provider metadata and hashes in pipeline ledgers.

## External storage

Set `ARTICLE_BACKUP_ROOT` to an encrypted external backup location. A backup
job should create dated snapshots containing:

- the raw Takeout archive;
- the `_article_pipeline/backups` rewrite originals;
- taxonomy migration manifests;
- completed-record state; and
- a checksum manifest and restore instructions.

The local repository keeps only the checksum, timestamp, source archive name,
retention class, and external snapshot identifier.

## Retention

- Keep raw Takeout and extracted-media snapshots externally for 90 days.
- Keep the most recent monthly snapshot for 12 months.
- Keep published-version backups until the corresponding article is retired.
- Delete local staging artifacts after 30 days, only after checksum verification.

Cleanup defaults to a dry run, accepts an explicit staging root, refuses
`articles/`, `data/`, `inbox/`, repository, home, and filesystem roots, and
writes a JSONL checksum report. Applying cleanup requires an external
`--backup-root`; each candidate is copied and rechecked before removal so the
report contains a restore path. Test a restore at least once per quarter and
after taxonomy migrations.
