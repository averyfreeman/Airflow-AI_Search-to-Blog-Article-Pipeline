#!/usr/bin/env python3
"""Small, auditable web-research adapter for article rewriting."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import re
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from codex_provider import PrivacyGateError, TextProvider
from prompt_policy import with_tone


@dataclasses.dataclass(frozen=True)
class ResearchSource:
    title: str
    url: str
    snippet: str
    extracted_text: str = ""


RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "claim_support": {"type": "string"},
                    "source_type": {"type": "string"},
                },
                "required": ["title", "url", "claim_support", "source_type"],
                "additionalProperties": False,
            },
        },
        "research_limits": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["query", "sources", "research_limits"],
    "additionalProperties": False,
}


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


def _parse_json_object(raw: str) -> dict[str, object]:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Research provider did not return a JSON object")
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("Research provider returned a non-object")
    return value


def _codex_research(
    title: str,
    brief: dict[str, object],
    provider: TextProvider,
    *,
    max_sources: int,
) -> dict[str, object]:
    query = _research_query(title, brief)
    prompt = with_tone(
        "Research the following article topic using live web search. Prefer official documentation, standards, "
        "primary sources, and authoritative local sources. Return only JSON matching the supplied schema. "
        "Capture source URLs and a short statement of what each source can support; do not turn search snippets "
        "into facts. Do not write unsupported conclusions, and mark time-sensitive or uncertain claims for review.\n\n"
        f"Article title: {title}\nSuggested query: {query}\nBrief:\n"
        f"{json.dumps(brief, ensure_ascii=False, indent=2)}\n\n"
        f"Limit the sources list to at most {max_sources} entries."
    )
    result = provider.complete(
        prompt,
        schema=RESEARCH_SCHEMA,
        operation="research",
        allow_web_search=True,
    )
    value = _parse_json_object(result.text)
    raw_sources = value.get("sources", [])
    sources: list[dict[str, str]] = []
    if isinstance(raw_sources, list):
        for item in raw_sources[:max_sources]:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", "")).strip()
            if not url.startswith(("https://", "http://")):
                continue
            sources.append(
                {
                    "title": str(item.get("title", "Untitled source")).strip(),
                    "url": url,
                    "claim_support": str(item.get("claim_support", "")).strip(),
                    "source_type": str(item.get("source_type", "web source")).strip(),
                }
            )
    limits = [str(item) for item in value.get("research_limits", []) if str(item).strip()]
    limits.extend(
        [
            "Codex web-search output is an evidence index; verify claims against each linked source.",
            "Time-sensitive, medical, legal, financial, and privacy-sensitive claims require editorial review.",
        ]
    )
    return {
        "query": str(value.get("query", query)),
        "sources": sources,
        "research_limits": list(dict.fromkeys(limits)),
        "provider": result.provider,
        "requested_model": result.requested_model,
        "actual_model": result.actual_model,
        "reasoning_effort": result.reasoning_effort,
        "run_id": result.run_id,
        "fallback_reason": result.fallback_reason,
        "citations": list(result.citations),
        "accessed_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }


def research_article(
    title: str,
    brief: dict[str, object],
    *,
    timeout: float = 20.0,
    max_sources: int = 4,
    client: httpx.Client | None = None,
    provider: TextProvider | None = None,
) -> dict[str, object]:
    """Collect a bounded evidence packet; snippets are never treated as facts."""
    query = _research_query(title, brief)
    if provider is not None:
        try:
            return _codex_research(title, brief, provider, max_sources=max_sources)
        except PrivacyGateError:
            return {
                "query": query,
                "sources": [],
                "research_limits": [
                    "Privacy gate blocked external research; route the article to editorial review."
                ],
                "provider": "privacy-gate",
                "accessed_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            }
        except Exception as exc:
            codex_failure = f"Codex research unavailable: {type(exc).__name__}: {exc}"
        else:
            codex_failure = ""
    else:
        codex_failure = ""
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
                *([codex_failure] if codex_failure else []),
                "Search results are discovery aids; verify claims against the linked source.",
                "Consider access date and source freshness for time-sensitive claims.",
            ],
        }
    except Exception as exc:
        return {
            "query": query,
            "sources": [],
            "research_limits": [
                *([codex_failure] if codex_failure else []),
                f"External research failed: {exc}",
                "Do not add unsupported facts; route the article to review.",
            ],
        }
    finally:
        if owned_client:
            session.close()
