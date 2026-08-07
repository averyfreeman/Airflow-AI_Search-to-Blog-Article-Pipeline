#!/usr/bin/env python3
"""Validate the controlled taxonomy and publication tree."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from taxonomy import CATEGORY_BY_LABEL, CATEGORY_BY_SLUG, MAX_CATEGORIES, parse_tags


FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
FENCE_RE = re.compile(r"^(```|~~~).*?^\1\s*$", re.M | re.S)


def validate_file(path: Path, source_dir: Path) -> list[str]:
    errors: list[str] = []
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(text)
    if not match:
        return [f"{path}: missing frontmatter"]
    frontmatter = match.group(1)
    tags = parse_tags(frontmatter)
    category_match = re.search(r"^Category:\s*[\"']?([^\"'\n]+)", frontmatter, re.M)
    category_value = category_match.group(1).strip() if category_match else ""
    if len(tags) != 1:
        errors.append(f"{path}: expected exactly one Tags value, found {tags!r}")
    if not category_value:
        errors.append(f"{path}: missing Category")
    category = CATEGORY_BY_LABEL.get(category_value.casefold()) or CATEGORY_BY_SLUG.get(category_value.casefold())
    if category is None:
        errors.append(f"{path}: unknown Category {category_value!r}")
    if tags and category and tags[0].casefold() != category.label.casefold():
        errors.append(f"{path}: Tags {tags[0]!r} does not match Category {category.label!r}")
    if category and path.parent.name != category.slug:
        errors.append(f"{path}: folder {path.parent.name!r} does not match {category.slug!r}")
    if len(text[match.end():].strip()) < 100:
        errors.append(f"{path}: article body is critically short")
    body_without_code = FENCE_RE.sub("", text[match.end():])
    if re.search(r"(?im)^\s*(?:your prompt|search(?:'s)? response|assistant(?: response)?|user)\s*:\s*$", body_without_code):
        errors.append(f"{path}: transcript marker remains in article body")
    return errors


def validate_tree(source_dir: Path) -> dict[str, object]:
    files = sorted(
        path for path in source_dir.rglob("*.md")
        if "_article_pipeline" not in path.parts and path.name != "article_surgeon_progress_tree.md"
    )
    errors: list[str] = []
    counts: Counter[str] = Counter()
    for path in files:
        errors.extend(validate_file(path, source_dir))
        match = FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
        if match:
            category_match = re.search(r"^Category:\s*[\"']?([^\"'\n]+)", match.group(1), re.M)
            if category_match:
                counts[category_match.group(1).strip()] += 1
    if len(counts) > MAX_CATEGORIES:
        errors.append(f"category count {len(counts)} exceeds hard maximum {MAX_CATEGORIES}")
    return {"files": len(files), "category_count": len(counts), "categories": dict(counts), "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, default=Path("articles"), nargs="?")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = validate_tree(args.source)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Validated {report['files']} articles across {report['category_count']} categories.")
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
