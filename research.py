#!/usr/bin/env python3
"""Small, auditable web-research adapter for article rewriting."""

from __future__ import annotations

import dataclasses
import os
import re
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup


@dataclasses.dataclass(frozen=True)
class ResearchSource:
    title: str
    url: str
    snippet: str
    extracted_text: str = ""


def _search_url(query: str) -> str:
    template = os.environ.get("ARTICLE_RESEARCH_SEARCH_URL", "https://html.duckduckgo.com/html/?q={query}")
    return template.format(query=quote(query))


def _research_query(title: str, brief: dict[str, object]) -> str:
    candidate = str(brief.get("topic") or title).strip()
    candidate = re.sub(r"\s+", " ", candidate).strip()
    stop_words = {
        "about", "after", "allow", "also", "and", "are", "can", "does", "for", "from", "have",
        "how", "into", "is", "it", "just", "more", "please", "that", "the", "this", "through",
        "using", "what", "when", "where", "which", "with", "you", "your",
    }
    terms: list[str] = []
    seen: set[str] = set()
    for term in re.findall(r"[A-Za-z][A-Za-z0-9_.@+/-]{2,}", candidate):
        key = term.casefold()
        if key in stop_words or key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) == 4:
            break
    subject = " ".join(terms) or title[:120]
    return f"{subject} official documentation standards"


def _source_url(href: str, base_url: str) -> str:
    parsed = urlparse(href)
    target = parse_qs(parsed.query).get("uddg", [None])[0]
    if target:
        return unquote(target)
    return urljoin(base_url, href)


def research_article(
    title: str,
    brief: dict[str, object],
    *,
    timeout: float = 20.0,
    max_sources: int = 4,
    client: httpx.Client | None = None,
) -> dict[str, object]:
    """Collect a bounded evidence packet; snippets are never treated as facts."""
    query = _research_query(title, brief)
    owned_client = client is None
    session = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        response = session.get(_search_url(query), headers={"User-Agent": "article-pipeline/1.0"})
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        sources: list[dict[str, str]] = []
        for result in soup.select(".result")[:max_sources]:
            anchor = result.select_one("a.result__a")
            if anchor is None or not anchor.get("href"):
                continue
            url = _source_url(str(anchor["href"]), str(response.url))
            snippet_node = result.select_one(".result__snippet")
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
            try:
                page = session.get(url, headers={"User-Agent": "article-pipeline/1.0"})
                page.raise_for_status()
                page_soup = BeautifulSoup(page.text, "html.parser")
                for node in page_soup(["script", "style", "nav", "footer", "header", "aside"]):
                    node.decompose()
                extracted = re.sub(r"\s+", " ", page_soup.get_text(" ", strip=True))[:6000]
                sources.append({"title": anchor.get_text(" ", strip=True), "url": url, "snippet": snippet, "extracted_text": extracted})
            except Exception as exc:
                sources.append({"title": anchor.get_text(" ", strip=True), "url": url, "snippet": snippet, "error": str(exc)})
        return {
            "query": query,
            "sources": sources,
            "research_limits": [
                "Search results are discovery aids; verify claims against the linked source.",
                "Consider access date and source freshness for time-sensitive claims.",
            ],
        }
    except Exception as exc:
        return {
            "query": query,
            "sources": [],
            "research_limits": [f"External research failed: {exc}", "Do not add unsupported facts; route the article to review."],
        }
    finally:
        if owned_client:
            session.close()
