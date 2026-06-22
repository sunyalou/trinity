from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.mark.asyncio
async def test_custom_provider_missing_config_without_network(monkeypatch):
    from services.provider_connection_test_service import ProviderConnectionTestService

    def fail_client(*args, **kwargs):
        raise AssertionError("network should not be called for custom providers")

    monkeypatch.setattr(httpx, "AsyncClient", fail_client)

    result = await ProviderConnectionTestService().test_connection("opencode", "local-llm", "my-model")

    assert result == {
        "ok": False,
        "status": "missing_config",
        "runtime": "opencode",
        "provider": "local-llm",
        "model": "my-model",
        "resolved_model": "local-llm/my-model",
        "message": "Custom provider base URL, API key, or protocol is missing.",
    }


@pytest.mark.asyncio
async def test_request_custom_provider_uses_model_endpoint_and_bearer_key(monkeypatch):
    import services.provider_connection_test_service as service_module

    captured = {}

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs["headers"]
            captured["timeout"] = kwargs["timeout"]
            return httpx.Response(200, text="{}")

    monkeypatch.setattr(service_module.settings_service, "get_custom_provider_configs", lambda: {})
    monkeypatch.setattr(httpx, "AsyncClient", lambda: DummyClient())

    result = await service_module.ProviderConnectionTestService().test_connection(
        "opencode",
        "local-llm",
        "my-model",
        {
            "protocol": "openai-compatible",
            "base_url": "https://example.test/v1/",
            "api_key": "request-secret",
        },
    )

    assert result["ok"] is True
    assert result["status"] == "connected"
    assert captured["url"] == "https://example.test/v1/models/my-model"
    assert captured["headers"] == {"Authorization": "Bearer request-secret"}
    assert captured["timeout"] == 10.0


@pytest.mark.asyncio
async def test_saved_config_fallback_and_models_list_fallback_connects(monkeypatch):
    import services.provider_connection_test_service as service_module

    calls = []

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, **kwargs):
            calls.append(("GET", url, kwargs))
            if url.endswith("/models/my-model"):
                return httpx.Response(404, text="missing")
            return httpx.Response(200, json={"data": [{"id": "my-model"}]})

    monkeypatch.setattr(
        service_module.settings_service,
        "get_custom_provider_configs",
        lambda: {"local-llm": {"protocol": "openai-compatible", "base_url": "https://saved.test/v1", "api_key": "saved-secret"}},
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda: DummyClient())

    result = await service_module.ProviderConnectionTestService().test_connection(
        "opencode", "local-llm", "my-model", {"api_key": "   "}
    )

    assert result["ok"] is True
    assert result["status"] == "connected"
    assert [call[1] for call in calls] == ["https://saved.test/v1/models/my-model", "https://saved.test/v1/models"]
    assert calls[1][2]["headers"] == {"Authorization": "Bearer saved-secret"}


@pytest.mark.asyncio
async def test_chat_completion_fallback_connects(monkeypatch):
    import services.provider_connection_test_service as service_module

    calls = []

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, **kwargs):
            calls.append(("GET", url, kwargs))
            if url.endswith("/models/my-model"):
                return httpx.Response(404, text="missing")
            return httpx.Response(200, json={"data": [{"id": "other-model"}]})

        async def post(self, url, **kwargs):
            calls.append(("POST", url, kwargs))
            return httpx.Response(200, text="{}")

    monkeypatch.setattr(service_module.settings_service, "get_custom_provider_configs", lambda: {})
    monkeypatch.setattr(httpx, "AsyncClient", lambda: DummyClient())

    result = await service_module.ProviderConnectionTestService().test_connection(
        "opencode",
        "local-llm",
        "my-model",
        {"protocol": "openai-compatible", "base_url": "https://example.test/v1", "api_key": "request-secret"},
    )

    assert result["ok"] is True
    assert result["status"] == "connected"
    assert calls[2][0] == "POST"
    assert calls[2][1] == "https://example.test/v1/chat/completions"
    assert calls[2][2]["headers"] == {"Authorization": "Bearer request-secret", "Content-Type": "application/json"}
    assert calls[2][2]["json"] == {"model": "my-model", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}


@pytest.mark.asyncio
async def test_chat_completion_fallback_model_error_maps_to_model_not_found_and_redacts(monkeypatch):
    import services.provider_connection_test_service as service_module

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, **kwargs):
            if url.endswith("/models/my-model"):
                return httpx.Response(404, text="missing")
            return httpx.Response(200, json={"data": [{"id": "other-model"}]})

        async def post(self, url, **kwargs):
            return httpx.Response(
                400,
                json={"error": {"code": "model_not_found", "type": "model_not_found_error", "message": "Model does not exist: my-model request-secret"}},
            )

    monkeypatch.setattr(service_module.settings_service, "get_custom_provider_configs", lambda: {})
    monkeypatch.setattr(httpx, "AsyncClient", lambda: DummyClient())

    result = await service_module.ProviderConnectionTestService().test_connection(
        "opencode",
        "local-llm",
        "my-model",
        {"protocol": "openai-compatible", "base_url": "https://example.test/v1", "api_key": "request-secret"},
    )

    assert result["ok"] is False
    assert result["status"] == "model_not_found"
    assert "request-secret" not in result["message"]
    assert "[REDACTED]" in result["message"]


@pytest.mark.asyncio
async def test_custom_provider_authentication_failure_redacts_request_secret(monkeypatch):
    import services.provider_connection_test_service as service_module

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, **kwargs):
            return httpx.Response(401, text="bad request-secret")

    monkeypatch.setattr(service_module.settings_service, "get_custom_provider_configs", lambda: {})
    monkeypatch.setattr(httpx, "AsyncClient", lambda: DummyClient())

    result = await service_module.ProviderConnectionTestService().test_connection(
        "opencode", "local-llm", "my-model", {"protocol": "openai-compatible", "base_url": "https://example.test/v1", "api_key": "request-secret"}
    )

    assert result["status"] == "authentication_failed"
    assert "request-secret" not in result["message"]
    assert "[REDACTED]" in result["message"]


@pytest.mark.asyncio
async def test_custom_provider_saved_key_error_redacts_saved_secret(monkeypatch):
    import services.provider_connection_test_service as service_module

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, **kwargs):
            return httpx.Response(403, text="bad saved-secret")

    monkeypatch.setattr(
        service_module.settings_service,
        "get_custom_provider_configs",
        lambda: {"local-llm": {"protocol": "openai-compatible", "base_url": "https://saved.test/v1", "api_key": "saved-secret"}},
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda: DummyClient())

    result = await service_module.ProviderConnectionTestService().test_connection("opencode", "local-llm", "my-model")

    assert result["status"] == "authentication_failed"
    assert "saved-secret" not in result["message"]
    assert "[REDACTED]" in result["message"]


@pytest.mark.asyncio
async def test_custom_provider_unknown_exception_redacts_secret(monkeypatch):
    import services.provider_connection_test_service as service_module

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, **kwargs):
            raise RuntimeError("boom request-secret")

    monkeypatch.setattr(service_module.settings_service, "get_custom_provider_configs", lambda: {})
    monkeypatch.setattr(httpx, "AsyncClient", lambda: DummyClient())

    result = await service_module.ProviderConnectionTestService().test_connection(
        "opencode", "local-llm", "my-model", {"protocol": "openai-compatible", "base_url": "https://example.test/v1", "api_key": "request-secret"}
    )

    assert result["status"] == "unknown_error"
    assert "request-secret" not in result["message"]
    assert "[REDACTED]" in result["message"]


@pytest.mark.asyncio
async def test_custom_provider_unsupported_protocol_returns_unsupported_provider(monkeypatch):
    import services.provider_connection_test_service as service_module

    def fail_client(*args, **kwargs):
        raise AssertionError("network should not be called for unsupported protocols")

    monkeypatch.setattr(service_module.settings_service, "get_custom_provider_configs", lambda: {})
    monkeypatch.setattr(httpx, "AsyncClient", fail_client)

    result = await service_module.ProviderConnectionTestService().test_connection(
        "opencode", "local-llm", "my-model", {"protocol": "anthropic-compatible", "base_url": "https://example.test/v1", "api_key": "request-secret"}
    )

    assert result["ok"] is False
    assert result["status"] == "unsupported_provider"


@pytest.mark.asyncio
async def test_missing_anthropic_credentials_maps_to_authentication_failed(monkeypatch):
    import services.provider_connection_test_service as service_module

    monkeypatch.setattr(service_module.settings_service, "get_anthropic_api_key", lambda: "")

    result = await service_module.ProviderConnectionTestService().test_connection(
        "claude-code", "anthropic", "claude-sonnet-4-6"
    )

    assert result["ok"] is False
    assert result["status"] == "authentication_failed"
    assert result["resolved_model"] == "anthropic/claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_anthropic_404_maps_to_model_not_found_and_redacts_key(monkeypatch):
    import services.provider_connection_test_service as service_module

    secret = "sk-ant-secret-value"
    captured = {}

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs["headers"]
            return httpx.Response(404, text=f"missing {secret}")

    monkeypatch.setattr(service_module.settings_service, "get_anthropic_api_key", lambda: secret)
    monkeypatch.setattr(httpx, "AsyncClient", lambda: DummyClient())

    result = await service_module.ProviderConnectionTestService().test_connection(
        "claude-code", "anthropic", "claude-missing"
    )

    assert result["ok"] is False
    assert result["status"] == "model_not_found"
    assert captured["url"] == "https://api.anthropic.com/v1/models/claude-missing"
    assert captured["headers"]["x-api-key"] == secret
    assert secret not in result["message"]


@pytest.mark.asyncio
async def test_openai_success_uses_exact_model_url_for_safe_model(monkeypatch):
    import services.provider_connection_test_service as service_module

    captured = {}

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs["headers"]
            captured["timeout"] = kwargs["timeout"]
            return httpx.Response(200, text="{}")

    monkeypatch.setattr(service_module.settings_service, "get_openai_api_key", lambda: "sk-openai-secret")
    monkeypatch.setattr(httpx, "AsyncClient", lambda: DummyClient())

    result = await service_module.ProviderConnectionTestService().test_connection(
        "opencode", "openai", "gpt-5-custom"
    )

    assert result["ok"] is True
    assert result["status"] == "connected"
    assert result["message"] == "Connection verified."
    assert captured["url"] == "https://api.openai.com/v1/models/gpt-5-custom"
    assert captured["headers"] == {"Authorization": "Bearer sk-openai-secret"}
    assert captured["timeout"] == 10.0


@pytest.mark.asyncio
async def test_timeout_maps_to_timed_out(monkeypatch):
    import services.provider_connection_test_service as service_module

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, **kwargs):
            raise httpx.TimeoutException("request timed out")

    monkeypatch.setattr(service_module.settings_service, "get_openai_api_key", lambda: "sk-openai-secret")
    monkeypatch.setattr(httpx, "AsyncClient", lambda: DummyClient())

    result = await service_module.ProviderConnectionTestService().test_connection("opencode", "openai", "gpt-5")

    assert result["ok"] is False
    assert result["status"] == "timed_out"


def test_validation_rejects_provider_containing_slash():
    from services.provider_connection_test_service import validate_provider_test_request

    with pytest.raises(ValueError, match="Provider cannot contain"):
        validate_provider_test_request("opencode", "bad/provider", "gpt-5")


@pytest.mark.parametrize("model", [" my-model", "my-model ", "\tmy-model", "my\tmodel", "bad model"])
def test_validation_rejects_custom_provider_model_whitespace(model):
    from services.provider_connection_test_service import validate_provider_test_request

    with pytest.raises(ValueError, match="Model cannot contain whitespace"):
        validate_provider_test_request("opencode", "local-llm", model)


@pytest.mark.parametrize("model", [" my-model", "my-model ", "\tmy-model", "my\tmodel"])
def test_validation_rejects_builtin_provider_model_whitespace(model):
    from services.provider_connection_test_service import validate_provider_test_request

    with pytest.raises(ValueError, match="Model cannot contain whitespace"):
        validate_provider_test_request("opencode", "openai", model)


@pytest.mark.parametrize("model", ["bad/model", "bad?model", "bad#model"])
def test_validation_rejects_builtin_provider_model_url_delimiters(model):
    from services.provider_connection_test_service import validate_provider_test_request

    with pytest.raises(ValueError, match="Model cannot contain URL delimiters"):
        validate_provider_test_request("opencode", "openai", model)


def test_fastapi_provider_connection_test_delegates_and_does_not_persist(monkeypatch):
    import routers.settings as settings_router
    from models import User

    calls = []
    persisted = []

    async def fake_test_connection(runtime, provider, model, custom_provider=None):
        calls.append((runtime, provider, model, custom_provider))
        return {
            "ok": True,
            "status": "connected",
            "runtime": runtime,
            "provider": provider,
            "model": model,
            "resolved_model": f"{provider}/{model}",
            "message": "Connection verified.",
        }

    app = FastAPI()
    app.include_router(settings_router.router)
    app.dependency_overrides[settings_router.get_current_user] = lambda: User(id=1, username="admin", role="admin")
    monkeypatch.setattr(settings_router.provider_connection_test_service, "test_connection", fake_test_connection)
    monkeypatch.setattr(settings_router.db, "set_setting", lambda *args, **kwargs: persisted.append((args, kwargs)))

    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/settings/provider-connection-test",
                json={
                    "runtime": "opencode",
                    "provider": "local-llm",
                    "model": "gpt-5",
                    "custom_provider": {
                        "protocol": "openai-compatible",
                        "base_url": "https://example.test/v1",
                        "api_key": "request-secret",
                    },
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "status": "connected",
        "runtime": "opencode",
        "provider": "local-llm",
        "model": "gpt-5",
        "resolved_model": "local-llm/gpt-5",
        "message": "Connection verified.",
    }
    assert calls == [
        (
            "opencode",
            "local-llm",
            "gpt-5",
            {"protocol": "openai-compatible", "base_url": "https://example.test/v1", "api_key": "request-secret"},
        )
    ]
    assert persisted == []


def test_fastapi_endpoint_rejects_invalid_runtime_provider_model_with_400(monkeypatch):
    import routers.settings as settings_router
    from models import User

    app = FastAPI()
    app.include_router(settings_router.router)
    app.dependency_overrides[settings_router.get_current_user] = lambda: User(id=1, username="admin", role="admin")

    try:
        with TestClient(app) as client:
            runtime_response = client.post(
                "/api/settings/provider-connection-test",
                json={"runtime": "vim", "provider": "openai", "model": "gpt-5"},
            )
            provider_response = client.post(
                "/api/settings/provider-connection-test",
                json={"runtime": "opencode", "provider": "bad/provider", "model": "gpt-5"},
            )
            model_response = client.post(
                "/api/settings/provider-connection-test",
                json={"runtime": "opencode", "provider": "openai", "model": "bad/model"},
            )
    finally:
        app.dependency_overrides.clear()

    assert runtime_response.status_code == 400
    assert provider_response.status_code == 400
    assert model_response.status_code == 400
