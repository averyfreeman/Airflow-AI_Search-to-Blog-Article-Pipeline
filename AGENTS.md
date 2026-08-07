

# Article content operating rules

This folder contains the published Markdown articles and the Python pipeline
that turns Google Takeout records into those articles.

## Taxonomy constraints

1. There is one controlled taxonomy in `taxonomy.py`. It is the only source of
   truth for article categories.
2. The publication tree must contain no more than 30 categories. The normal
   target is 23; validation must fail before the hard limit is reached.
3. Every published article must have exactly one `Category` and exactly one
   `Tags` value. The values must use an allowlisted category label.
4. The article's parent folder must equal the category slug. For example,
   `Category: "Linux and Unix"` belongs in `articles/linux-unix/`.
5. `Search`, `AI_Mode`, `Gemini Apps`, product names, technologies, places,
   and other subjects are not categories. Preserve source provenance in
   `Source_Products` or other explicit metadata, never by creating a tag or
   folder.
6. Adding a category requires changing `taxonomy.py`, documenting the reason,
   adding or updating validation tests, and proving the total remains below 30.
7. Never create a one-off subject folder. Ambiguous articles go to the broadest
   existing category and are recorded for editorial review.

## Article quality constraints

1. Published articles must be readable prose, not a transcript. Remove speaker
   labels, prompt/response scaffolding, and staccato fragments.
2. External facts must be supported by recorded research sources. Prefer
   official documentation, standards, primary sources, and authoritative local
   sources. Do not invent commands, citations, prices, policies, or medical,
   legal, or financial conclusions.
3. Research uncertainty, privacy concerns, and time-sensitive claims require
   editorial review before publication.
4. Every generated article must pass `python3 validate_content.py articles`.

## Storage and backups

1. Raw Takeout archives, extracted media, and full model artifacts do not
   belong in the published article tree.
2. Store durable raw data and rewrite backups in the configured external backup
   location. Keep only checksummed manifests and short-lived staging artifacts
   locally.
3. Destructive cleanup requires a dry run, explicit paths, checksum/manifest
   verification, and a documented restore path. Never delete published
   Markdown as part of raw-data cleanup.

## Required checks

Before publishing or moving content, run:

```sh
python3 migrate_taxonomy.py --source articles --dry-run
python3 validate_content.py articles
python3 -m pytest tests
```

The migration manifest is the audit record for category and path changes.
