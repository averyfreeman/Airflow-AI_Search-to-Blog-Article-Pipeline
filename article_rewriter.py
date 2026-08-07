#!/usr/bin/env python3
"""Rewrite scored Takeout-derived Markdown into coherent single-author articles."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import inspect
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

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
from public_interest_scorer import DEFAULT_LOCAL_BASE_URL, chunk_markdown
from research import research_article


FENCE_RE = re.compile(r"(^```.*?$.*?^```\s*$|^~~~.*?$.*?^~~~\s*$)", re.M | re.S)
LINK_RE = re.compile(r"\[([^\]]+)]\(([^)]+)\)")
TRANSCRIPT_MARKER_RE = re.compile(
    r"^\s*(?:your prompt|search(?:'s)? response|assistant(?: response)?|user)\s*:\s*$",
    re.I | re.M,
)
DEFAULT_MAX_INPUT_TOKENS = 40_000


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def protect_markdown(text: str) -> tuple[str, dict[str, str]]:
    protected: dict[str, str] = {}

    def stash(prefix: str, value: str) -> str:
        token = f"__{prefix}_{len(protected):05d}__"
        protected[token] = value
        return token

    text = FENCE_RE.sub(lambda match: stash("PRESERVED_CODE", match.group(0)), text)

    def protect_link(match: re.Match[str]) -> str:
        anchor, target = match.group(1), match.group(2)
        token = stash("PRESERVED_URL", target)
        return f"[{anchor}]({token})"

    return LINK_RE.sub(protect_link, text), protected


def restore_markdown(text: str, protected: dict[str, str]) -> str:
    missing = [token for token in protected if token not in text]
    if missing:
        raise ValueError(f"Rewrite omitted {len(missing)} protected Markdown token(s): {missing[:3]}")
    for token, value in protected.items():
        text = text.replace(token, value)
    return text


def effective_context(base_url: str, timeout: float = 10.0) -> int:
    response = httpx.get(base_url.removesuffix("/v1").rstrip("/") + "/props", timeout=timeout)
    response.raise_for_status()
    return int(response.json()["default_generation_settings"]["n_ctx"])


def score_record_from_article(article: Path, source_dir: Path) -> Path:
    text = article.read_text(encoding="utf-8")
    configured = frontmatter_value(text, "Score_Record")
    if configured:
        candidate = source_dir / configured
        if candidate.is_file():
            return candidate
    digest = hashlib.sha256(str(article.resolve()).encode("utf-8")).hexdigest()[:20]
    candidate = source_dir / "_article_pipeline" / "scores" / f"{digest}.score.json"
    if not candidate.is_file():
        raise FileNotFoundError(f"No score record for {article}")
    return candidate


def load_score_record(article: Path, source_dir: Path) -> tuple[dict[str, Any], Path]:
    path = score_record_from_article(article, source_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("disposition") != "rewrite":
        raise ValueError(f"Article is not in the rewrite queue: {data.get('disposition')}")
    if not isinstance(data.get("brief"), dict):
        raise ValueError(f"Score record has no document brief: {path}")
    return data, path


class LocalRewriteClient:
    def __init__(self, base_url: str = DEFAULT_LOCAL_BASE_URL, timeout: float = 600.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.model = discover_single_model(self.base_url)
        self.context = effective_context(self.base_url)

    def rewrite(
        self,
        chunk: str,
        brief: dict[str, Any],
        *,
        index: int,
        total: int,
        required_tokens: list[str] | None = None,
    ) -> str:
        token_instruction = ""
        if required_tokens:
            token_instruction = (
                " The section contains protected placeholders. They are not optional: preserve every token "
                "verbatim, in the same relative location, and do not explain or rename them. Required tokens: "
                + ", ".join(required_tokens)
                + "."
            )
        system = (
            "You are a careful technical editor. Rewrite raw conversation-derived Markdown into a coherent "
            "single-author article section written for a real reader. Build a clear problem, context, explanation, "
            "resolution, and takeaway arc; use connected paragraphs and natural transitions rather than fragments. "
            "Preserve every placeholder exactly, retain factual uncertainty, commands, code, links, and useful details. "
            "Remove speaker labels, repeated prompt scaffolding, and chatbot follow-up offers. External research is "
            "evidence to verify, not permission to invent facts: use only claims supported by the source or linked "
            "research, and flag conflicts or missing evidence. Return only Markdown for this section."
            + token_instruction
        )
        user = (
            f"Global document brief shared by every section:\n{json.dumps(brief, ensure_ascii=False, indent=2)}\n\n"
            f"Rewrite section {index} of {total}. Keep its relationship to the complete article arc clear. "
            "Do not add a new top-level title unless one is present in this section.\n\n"
            f"Source Markdown:\n{chunk}"
        )
        response = httpx.post(
            self.base_url + "/chat/completions",
            json={
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0.2,
                "max_tokens": min(12_000, max(2_000, self.context // 5)),
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        return strip_thought_channels(response.json()["choices"][0]["message"]["content"])


def validate_rewrite(original_body: str, rewritten_body: str, protected: dict[str, str]) -> None:
    if len(rewritten_body.strip()) < 100:
        raise ValueError("Rewritten article is critically short")
    if protected and any(value not in rewritten_body for value in protected.values()):
        raise ValueError("Rewritten article failed protected Markdown restoration")
    original_code = len(FENCE_RE.findall(original_body))
    rewritten_code = len(FENCE_RE.findall(rewritten_body))
    if rewritten_code != original_code:
        raise ValueError(f"Fenced code block count changed: {original_code} -> {rewritten_code}")
    if TRANSCRIPT_MARKER_RE.search(rewritten_body):
        raise ValueError("Rewritten article still contains transcript speaker markers")


def backup_article(article: Path, source_dir: Path, original: str) -> Path:
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:12]
    relative = article.relative_to(source_dir)
    target = source_dir / "_article_pipeline" / "backups" / relative.parent / f"{article.stem}.{digest}.pre-rewrite.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        atomic_write(target, original)
    return target


def append_manifest(source_dir: Path, event: dict[str, Any]) -> None:
    path = source_dir / "_article_pipeline" / "rewrite_manifest.jsonl"
    append_jsonl(path, {"timestamp": utc_now(), **event})


def rewrite_file(
    article: Path,
    *,
    source_dir: Path,
    client: LocalRewriteClient,
    dry_run: bool = False,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    research: bool = False,
) -> dict[str, Any]:
    score_record, score_path = load_score_record(article, source_dir)
    original = article.read_text(encoding="utf-8")
    _, body = split_frontmatter(original)
    brief = dict(score_record["brief"])
    research_path: Path | None = None
    if research:
        title = frontmatter_value(original, "Title") or article.stem
        research_packet = research_article(title, brief)
        brief["external_research"] = research_packet
        digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:12]
        research_path = source_dir / "_article_pipeline" / "research" / f"{article.stem}.{digest}.research.json"
        if not dry_run:
            atomic_write(research_path, json.dumps(research_packet, ensure_ascii=False, indent=2) + "\n")
    protected_body, protected = protect_markdown(body)
    safe_limit = min(max_input_tokens, max(8_000, client.context - 20_000))
    chunks = chunk_markdown(protected_body, client.base_url, max_tokens=safe_limit)
    supports_token_inventory = "required_tokens" in inspect.signature(client.rewrite).parameters
    rewritten_chunks = []
    for index, chunk in enumerate(chunks, start=1):
        kwargs = {"index": index, "total": len(chunks)}
        if supports_token_inventory:
            kwargs["required_tokens"] = [token for token in protected if token in chunk]
        rewritten_chunks.append(client.rewrite(chunk, brief, **kwargs))
    restored = restore_markdown("\n\n".join(rewritten_chunks).strip(), protected)
    validate_rewrite(body, restored, protected)
    rewritten_at = utc_now()
    output = update_frontmatter(
        original,
        {
            "Rewritten_At": rewritten_at,
            "Rewrite_Model": Path(client.model).name,
            "Rewrite_Brief": str(score_path.relative_to(source_dir)),
        },
    )
    frontmatter, _ = split_frontmatter(output)
    output = f"---\n{frontmatter}\n---\n{restored.rstrip()}\n"
    backup = source_dir / "_article_pipeline" / "backups" / "dry-run"
    if not dry_run:
        backup = backup_article(article, source_dir, original)
        atomic_write(article, output)
        append_manifest(
            source_dir,
            {
                "event": "article_rewritten",
                "article": str(article.resolve()),
                "score_record": str(score_path),
                "backup": str(backup),
                "model": client.model,
                "context_tokens": client.context,
                "chunks": len(chunks),
                "research": str(research_path) if research_path else None,
            },
        )
    return {
        "article": str(article),
        "backup": str(backup),
        "model": client.model,
        "context_tokens": client.context,
        "chunks": len(chunks),
        "research": str(research_path) if research_path else None,
        "dry_run": dry_run,
    }


def iter_rewrite_queue(source_dir: Path) -> Iterable[Path]:
    for article in sorted(source_dir.rglob("*.md")):
        if "_article_pipeline" in article.parts or "backup" in article.parts:
            continue
        if frontmatter_value(article.read_text(encoding="utf-8"), "Score_Disposition") == "rewrite":
            yield article


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("./staging"))
    parser.add_argument("--article", type=Path, action="append")
    parser.add_argument("--local-base-url", default=DEFAULT_LOCAL_BASE_URL)
    parser.add_argument("--max-input-tokens", type=int, default=DEFAULT_MAX_INPUT_TOKENS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--research", action="store_true", help="Gather a bounded external evidence packet before rewriting")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    client = LocalRewriteClient(args.local_base_url)
    articles = args.article or list(iter_rewrite_queue(args.source))
    results = [
        rewrite_file(
            article,
            source_dir=args.source,
            client=client,
            dry_run=args.dry_run,
            max_input_tokens=args.max_input_tokens,
            research=args.research,
        )
        for article in articles
    ]
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
