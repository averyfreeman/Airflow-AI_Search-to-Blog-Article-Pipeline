import threading
from http.server import ThreadingHTTPServer

import httpx

from codex_gateway import GatewayHandler
from codex_provider import ProviderResult


class FakeGatewayProvider:
    def complete(self, prompt: str, **kwargs):
        return ProviderResult(
            text='{"ok": true}',
            provider="fake",
            requested_model="gpt-5.6-luna",
            actual_model="gpt-5.6-luna",
            reasoning_effort="max",
            run_id="run-test",
            prompt_sha256="prompt",
            output_sha256="output",
        )


def test_gateway_requires_bearer_token_and_returns_provider_metadata() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
    server.gateway_token = "test-token"  # type: ignore[attr-defined]
    server.gateway_provider = FakeGatewayProvider()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        unauthorized = httpx.post(url + "/v1/generate", json={"prompt": "safe"})
        assert unauthorized.status_code == 401
        response = httpx.post(
            url + "/v1/generate",
            headers={"Authorization": "Bearer test-token"},
            json={"prompt": "safe", "operation": "score"},
        )
        assert response.status_code == 200
        assert response.json()["requested_model"] == "gpt-5.6-luna"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
