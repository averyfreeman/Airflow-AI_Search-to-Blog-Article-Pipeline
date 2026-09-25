"""Provider seam for Codex-authenticated model work.

The primary provider launches the local Codex app-server through the official
Python SDK.  ChatGPT OAuth is owned by Codex; this module never reads or writes
Codex credential files and never turns an OAuth token into a direct API call.
When the primary runtime is unavailable, the configured local OpenAI-compatible
server is used and the fallback reason is retained in the result metadata.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any, Protocol

import httpx

from privacy_filter import PrivacyDecision, sanitize_for_model
from prompt_policy import MODEL_ID, REASONING_EFFORT, prompt_hash


class ProviderUnavailable(RuntimeError):
    """Raised when a provider cannot complete a request."""


class PrivacyGateError(ProviderUnavailable):
    """Raised when model input contains blocked sensitive material."""


@dataclasses.dataclass(frozen=True)
class ProviderResult:
    text: str
    provider: str
    requested_model: str
    actual_model: str
    reasoning_effort: str
    run_id: str | None
    prompt_sha256: str
    output_sha256: str
    fallback_reason: str | None = None
    citations: tuple[dict[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self) | {"citations": list(self.citations)}


class TextProvider(Protocol):
    model: str

    def complete(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        operation: str = "article",
        allow_web_search: bool = False,
        privacy: PrivacyDecision | None = None,
    ) -> ProviderResult: ...


def _sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _final_text(result: Any) -> str:
    value = getattr(result, "final_response", None)
    if value is None and isinstance(result, dict):
        value = result.get("final_response") or result.get("text")
    if value is None:
        raise ProviderUnavailable("Codex completed without a final response")
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _result_model(result: Any, requested: str) -> str:
    usage = getattr(result, "usage", None)
    model = getattr(usage, "model", None) if usage is not None else None
    if isinstance(usage, dict):
        model = usage.get("model") or usage.get("actual_model")
    return str(model or requested)


def _extract_citations(result: Any) -> tuple[dict[str, str], ...]:
    """Extract URL/title metadata without retaining model reasoning."""

    items = getattr(result, "items", None) or []
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        raw = item.model_dump() if hasattr(item, "model_dump") else item
        if not isinstance(raw, dict):
            continue
        blob = json.dumps(raw, ensure_ascii=False)
        for token in blob.split('"'):
            if token.startswith(("https://", "http://")) and token not in seen:
                seen.add(token)
                found.append({"url": token})
    return tuple(found)


class CodexProvider:
    """Run one isolated, read-only Codex SDK turn per request."""

    provider_name = "codex-app-server"

    def __init__(
        self,
        *,
        model: str = MODEL_ID,
        reasoning_effort: str = REASONING_EFFORT,
        codex_bin: str | None = None,
    ) -> None:
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.codex_bin = codex_bin

    def complete(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        operation: str = "article",
        allow_web_search: bool = False,
        privacy: PrivacyDecision | None = None,
    ) -> ProviderResult:
        decision = privacy or sanitize_for_model(prompt)
        if decision.blocked:
            raise PrivacyGateError("Model input was blocked by the privacy gate")
        safe_prompt = decision.text
        try:
            from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox
        except ImportError as exc:
            raise ProviderUnavailable("Install openai-codex to use the Codex provider") from exc

        config = CodexConfig(codex_bin=self.codex_bin) if self.codex_bin else None
        try:
            with Codex(config=config) as codex:
                thread_config = {"web_search": "live"} if allow_web_search else None
                thread = codex.thread_start(
                    model=self.model,
                    ephemeral=True,
                    sandbox=Sandbox.read_only,
                    approval_mode=ApprovalMode.deny_all,
                    config=thread_config,
                )
                result = thread.run(
                    safe_prompt,
                    model=self.model,
                    effort=self.reasoning_effort,
                    output_schema=schema,
                    sandbox=Sandbox.read_only,
                    source=operation,
                )
        except Exception as exc:
            raise ProviderUnavailable(f"Codex app-server request failed: {type(exc).__name__}: {exc}") from exc

        text = _final_text(result)
        return ProviderResult(
            text=text,
            provider=self.provider_name,
            requested_model=self.model,
            actual_model=_result_model(result, self.model),
            reasoning_effort=self.reasoning_effort,
            run_id=str(getattr(result, "id", "")) or None,
            prompt_sha256=prompt_hash(safe_prompt),
            output_sha256=_sha256(text),
            citations=_extract_citations(result),
        )


class LocalCompatibleProvider:
    """Adapter for the existing local OpenAI-compatible server."""

    provider_name = "local-openai-compatible"

    def __init__(self, base_url: str, *, model: str | None = None, timeout: float = 600.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model or os.environ.get("LOCAL_LLM_MODEL", "")
        self.timeout = timeout

    def _model_id(self) -> str:
        if self.model and self.model != "auto":
            return self.model
        response = httpx.get(self.base_url + "/models", timeout=min(self.timeout, 15.0))
        response.raise_for_status()
        models = [str(item["id"]) for item in response.json().get("data", []) if item.get("id")]
        if len(models) != 1:
            raise ProviderUnavailable(f"Expected one local model, found {len(models)}")
        return models[0]

    def complete(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        operation: str = "article",
        allow_web_search: bool = False,
        privacy: PrivacyDecision | None = None,
    ) -> ProviderResult:
        decision = privacy or sanitize_for_model(prompt)
        if decision.blocked:
            raise PrivacyGateError("Model input was blocked by the privacy gate")
        model = self._model_id()
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": decision.text}],
            "temperature": 0.0,
            "max_tokens": 12000,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": operation.replace("-", "_"), "strict": True, "schema": schema},
            }
        try:
            response = httpx.post(self.base_url + "/chat/completions", json=payload, timeout=self.timeout)
            response.raise_for_status()
            text = str(response.json()["choices"][0]["message"]["content"])
        except Exception as exc:
            raise ProviderUnavailable(f"Local model request failed: {type(exc).__name__}: {exc}") from exc
        return ProviderResult(
            text=text,
            provider=self.provider_name,
            requested_model=model,
            actual_model=model,
            reasoning_effort="none",
            run_id=None,
            prompt_sha256=prompt_hash(decision.text),
            output_sha256=_sha256(text),
        )


class CodexGatewayClient:
    """Bearer-authenticated client for a host-side Codex gateway."""

    provider_name = "codex-gateway"

    def __init__(self, base_url: str, token_file: Path, *, timeout: float = 900.0, model: str = MODEL_ID) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_file = token_file
        self.timeout = timeout
        self.model = model

    def complete(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        operation: str = "article",
        allow_web_search: bool = False,
        privacy: PrivacyDecision | None = None,
    ) -> ProviderResult:
        decision = privacy or sanitize_for_model(prompt)
        if decision.blocked:
            raise PrivacyGateError("Model input was blocked by the privacy gate")
        token = self.token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise ProviderUnavailable("Codex gateway token file is empty")
        payload = {
            "prompt": decision.text,
            "schema": schema,
            "operation": operation,
            "allow_web_search": allow_web_search,
            "model": self.model,
            "reasoning_effort": REASONING_EFFORT,
        }
        try:
            response = httpx.post(
                self.base_url + "/v1/generate",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise ProviderUnavailable(f"Codex gateway request failed: {type(exc).__name__}: {exc}") from exc
        return ProviderResult(
            text=str(data["text"]),
            provider=str(data.get("provider", self.provider_name)),
            requested_model=str(data.get("requested_model", self.model)),
            actual_model=str(data.get("actual_model", self.model)),
            reasoning_effort=str(data.get("reasoning_effort", REASONING_EFFORT)),
            run_id=data.get("run_id"),
            prompt_sha256=str(data.get("prompt_sha256", prompt_hash(decision.text))),
            output_sha256=str(data.get("output_sha256", _sha256(str(data["text"])))),
            fallback_reason=data.get("fallback_reason"),
            citations=tuple(data.get("citations", ())),
        )


class ResilientProvider:
    """Primary provider with explicit, auditable local fallback."""

    def __init__(self, primary: TextProvider, fallback: TextProvider | None = None) -> None:
        self.primary = primary
        self.fallback = fallback
        self.model = primary.model

    def complete(self, prompt: str, **kwargs: Any) -> ProviderResult:
        try:
            return self.primary.complete(prompt, **kwargs)
        except PrivacyGateError:
            raise
        except Exception as exc:
            if self.fallback is None:
                raise
            result = self.fallback.complete(prompt, **kwargs)
            return dataclasses.replace(
                result,
                requested_model=self.primary.model,
                fallback_reason=f"{type(exc).__name__}: {exc}",
            )


def runtime_provider_from_environment() -> ResilientProvider:
    """Build the host or Docker provider selected by environment variables."""

    mode = os.environ.get("CODEX_RUNTIME", "host").strip().lower()
    model = os.environ.get("CODEX_MODEL", MODEL_ID).strip() or MODEL_ID
    if mode == "gateway":
        primary: TextProvider = CodexGatewayClient(
            os.environ.get("CODEX_GATEWAY_URL", "http://host.docker.internal:8787"),
            Path(os.environ.get("CODEX_GATEWAY_TOKEN_FILE", "/run/secrets/codex_gateway_token")),
            model=model,
        )
    else:
        primary = CodexProvider(model=model, reasoning_effort=os.environ.get("CODEX_REASONING_EFFORT", REASONING_EFFORT))
    fallback: TextProvider | None = None
    if os.environ.get("CODEX_LOCAL_FALLBACK", "true").lower() in {"1", "true", "yes"}:
        fallback = LocalCompatibleProvider(
            os.environ.get("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11400/v1"),
            model=os.environ.get("LOCAL_LLM_MODEL"),
        )
    return ResilientProvider(primary, fallback)
