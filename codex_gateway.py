#!/usr/bin/env python3
"""Small host-only gateway for Docker Airflow workers.

The gateway is deliberately narrower than a general OpenAI proxy: it accepts
one sanitized prompt, a bounded JSON schema, and an operation label, then runs
the same Codex SDK provider used by host workers.  It does not expose OAuth
tokens, the Codex account directory, filesystem tools, or arbitrary commands.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from codex_provider import CodexProvider, LocalCompatibleProvider, ResilientProvider
from prompt_policy import MODEL_ID, REASONING_EFFORT


MAX_REQUEST_BYTES = 2_000_000
ALLOWED_OPERATIONS = {"brief", "score", "rewrite", "article-surgeon", "research", "vision", "article"}


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "article-pipeline-codex-gateway/1"

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.gateway_token}"  # type: ignore[attr-defined]
        return secrets.compare_digest(supplied, expected)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._json(HTTPStatus.OK, {"status": "ok"})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/generate":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request too large"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            prompt = str(payload["prompt"])
            operation = str(payload.get("operation", "article"))
            if operation not in ALLOWED_OPERATIONS:
                raise ValueError("unsupported operation")
            schema = payload.get("schema")
            if schema is not None and not isinstance(schema, dict):
                raise ValueError("schema must be an object")
            result = self.server.gateway_provider.complete(  # type: ignore[attr-defined]
                prompt,
                schema=schema,
                operation=operation,
                allow_web_search=bool(payload.get("allow_web_search", False)),
            )
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except Exception as exc:  # Do not send prompt contents or tracebacks to the client.
            self._json(HTTPStatus.BAD_GATEWAY, {"error": f"provider failure: {type(exc).__name__}"})
            return
        self._json(HTTPStatus.OK, result.as_dict())

    def log_message(self, format: str, *args: object) -> None:
        # Keep prompts and model output out of the gateway's default logs.
        super().log_message("request handled")


def serve(host: str, port: int, token_file: Path) -> None:
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError(f"Gateway token file is empty: {token_file}")
    server = ThreadingHTTPServer((host, port), GatewayHandler)
    server.gateway_token = token  # type: ignore[attr-defined]
    # The gateway is the host-side endpoint; never inherit CODEX_RUNTIME=gateway
    # from a container environment and accidentally proxy back to itself.
    fallback = None
    if os.environ.get("CODEX_LOCAL_FALLBACK", "true").lower() in {"1", "true", "yes"}:
        fallback = LocalCompatibleProvider(
            os.environ.get("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11400/v1"),
            model=os.environ.get("LOCAL_LLM_MODEL"),
        )
    server.gateway_provider = ResilientProvider(  # type: ignore[attr-defined]
        CodexProvider(
            model=os.environ.get("CODEX_MODEL", MODEL_ID),
            reasoning_effort=os.environ.get("CODEX_REASONING_EFFORT", REASONING_EFFORT),
        ),
        fallback,
    )
    print(f"Codex gateway listening on {host}:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("CODEX_GATEWAY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CODEX_GATEWAY_PORT", "8787")))
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path(os.environ.get("CODEX_GATEWAY_TOKEN_FILE", ".codex-gateway-token")),
    )
    args = parser.parse_args(argv)
    serve(args.host, args.port, args.token_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
