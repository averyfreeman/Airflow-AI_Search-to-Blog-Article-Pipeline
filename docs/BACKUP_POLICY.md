# Article pipeline backup policy

## What stays here

Keep published Markdown, Python source, tests, small JSONL state/manifests, and
the current short-lived staging window. Do not keep raw Takeout exports,
extracted attachments, PDFs, images, audio, or full model transcripts in the
publication tree.

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

Cleanup must support `--dry-run`, accept explicit roots, refuse to target
`articles/`, and write a JSONL deletion report. Test a restore at least once per
quarter and after taxonomy migrations.
