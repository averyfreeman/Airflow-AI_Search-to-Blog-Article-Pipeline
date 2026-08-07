# Article taxonomy

The publication taxonomy is intentionally broad. It is designed for useful
navigation and stable word clouds, not for representing every subject named in
an article.

The canonical list and classifier live in `taxonomy.py`. Articles have one
category only, represented in both frontmatter and the parent directory:

```yaml
Category: "Linux and Unix"
Tags:
  - Linux and Unix
```

The corresponding directory is `articles/linux-unix/`.

`Source_Products` records whether the article originated in Google Search,
AI Mode, or another Takeout product. It is provenance, not taxonomy. Specific
technologies and concepts stay in the article title/body and must not become
new folders.

The target is 23 categories and the hard maximum is 30. Any new category must
be justified as a durable reader-facing domain, added to the allowlist, tested,
and reviewed before publication.
