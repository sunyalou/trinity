from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from services.settings_service import settings_service
from services.runtime_model_defaults import SUPPORTED_RUNTIMES, resolve_provider_model
from services.custom_provider_configs import OPENAI_COMPATIBLE_PROTOCOL, validate_custom_provider_configs

BUILT_IN_PROVIDERS = {"anthropic", "openai", "google"}
_WHITESPACE_RE = re.compile(r"\s")
_URL_DELIMITER_RE = re.compile(r"[/?#]")


def validate_provider_test_request(runtime: str, provider: str, model: str) -> dict[str, str]:
    runtime = str(runtime or "").strip()
    provider = str(provider or "").strip()
    raw_model = str(model or "")
    model = raw_model.strip()

    if runtime not in SUPPORTED_RUNTIMES:
        raise ValueError(f"Unsupported runtime: {runtime}")
    if not provider:
        raise ValueError("Provider is required")
    if "/" in provider:
        raise ValueError("Provider cannot contain '/'")
    if _WHITESPACE_RE.search(provider):
        raise ValueError("Provider cannot contain whitespace")
    if not model:
        raise ValueError("Model is required")
    if raw_model != model or _WHITESPACE_RE.search(model):
        raise ValueError("Model cannot contain whitespace")
    if provider in BUILT_IN_PROVIDERS and _URL_DELIMITER_RE.search(model):
        raise ValueError("Model cannot contain URL delimiters for built-in providers")

    return {"runtime": runtime, "provider": provider, "model": model}


def _sanitize_message(message: str, secrets: list[str]) -> str:
    sanitized = str(message)
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "[REDACTED]")
    return sanitized


class ProviderConnectionTestService:
    async def test_connection(
        self,
        runtime: str,
        provider: str,
        model: str,
        custom_provider: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = validate_provider_test_request(runtime, provider, model)
        runtime = normalized["runtime"]
        provider = normalized["provider"]
        model = normalized["model"]
        resolved_model = resolve_provider_model({"provider": provider, "model": model})

        if provider not in BUILT_IN_PROVIDERS:
            return await self._test_custom_provider_connection(runtime, provider, model, resolved_model, custom_provider)

        key = self._get_key(provider)
        if not key:
            return self._result(
                False,
                "authentication_failed",
                runtime,
                provider,
                model,
                resolved_model,
                f"Missing {provider} credentials.",
            )

        try:
            response = await self._request_model(provider, model, key)
            status, message = self._map_response(response, [key])
            return self._result(
                status == "connected",
                status,
                runtime,
                provider,
                model,
                resolved_model,
                message,
            )
        except httpx.TimeoutException as e:
            return self._result(False, "timed_out", runtime, provider, model, resolved_model, _sanitize_message(str(e) or "Request timed out.", [key]))
        except httpx.ConnectError as e:
            return self._result(False, "provider_unreachable", runtime, provider, model, resolved_model, _sanitize_message(str(e) or "Provider unreachable.", [key]))
        except Exception as e:
            return self._result(False, "unknown_error", runtime, provider, model, resolved_model, _sanitize_message(str(e), [key]))

    def _get_key(self, provider: str) -> str:
        if provider == "anthropic":
            return settings_service.get_anthropic_api_key()
        if provider == "openai":
            return settings_service.get_openai_api_key()
        if provider == "google":
            return settings_service.get_google_api_key()
        return ""

    async def _request_model(self, provider: str, model: str, key: str) -> httpx.Response:
        encoded_model = quote(model, safe="")
        async with httpx.AsyncClient() as client:
            if provider == "anthropic":
                return await client.get(
                    f"https://api.anthropic.com/v1/models/{encoded_model}",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                    timeout=10.0,
                )
            if provider == "openai":
                return await client.get(
                    f"https://api.openai.com/v1/models/{encoded_model}",
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=10.0,
                )
            return await client.get(
                f"https://generativelanguage.googleapis.com/v1beta/models/{encoded_model}",
                params={"key": key},
                timeout=10.0,
            )

    async def _test_custom_provider_connection(
        self,
        runtime: str,
        provider: str,
        model: str,
        resolved_model: str,
        custom_provider: dict[str, Any] | None,
    ) -> dict[str, Any]:
        saved_configs = settings_service.get_custom_provider_configs()
        saved_config = saved_configs.get(provider, {}) if isinstance(saved_configs, dict) else {}
        if not isinstance(saved_config, dict):
            saved_config = {}
        request_config = custom_provider if isinstance(custom_provider, dict) else {}

        secrets = []
        for config in (saved_config, request_config):
            api_key = config.get("api_key") if isinstance(config, dict) else None
            if isinstance(api_key, str) and api_key.strip():
                secrets.append(api_key.strip())

        merged = self._merge_custom_provider_config(saved_config, request_config)
        if not merged.get("protocol") or not merged.get("base_url") or not merged.get("api_key"):
            return self._result(
                False,
                "missing_config",
                runtime,
                provider,
                model,
                resolved_model,
                "Custom provider base URL, API key, or protocol is missing.",
            )
        if merged["protocol"] != OPENAI_COMPATIBLE_PROTOCOL:
            return self._result(
                False,
                "unsupported_provider",
                runtime,
                provider,
                model,
                resolved_model,
                f"Unsupported custom provider protocol: {merged['protocol']}.",
            )

        try:
            config = validate_custom_provider_configs({provider: merged})[provider]
        except ValueError as e:
            return self._result(
                False,
                "missing_config",
                runtime,
                provider,
                model,
                resolved_model,
                _sanitize_message(str(e) or "Custom provider base URL, API key, or protocol is missing.", secrets),
            )

        secrets.append(config["api_key"])
        try:
            status, message = await self._request_openai_compatible(config["base_url"], model, config["api_key"], secrets)
            return self._result(status == "connected", status, runtime, provider, model, resolved_model, message)
        except httpx.TimeoutException as e:
            return self._result(False, "timed_out", runtime, provider, model, resolved_model, _sanitize_message(str(e) or "Request timed out.", secrets))
        except httpx.ConnectError as e:
            return self._result(False, "provider_unreachable", runtime, provider, model, resolved_model, _sanitize_message(str(e) or "Provider unreachable.", secrets))
        except Exception as e:
            return self._result(False, "unknown_error", runtime, provider, model, resolved_model, _sanitize_message(str(e), secrets))

    def _merge_custom_provider_config(self, saved_config: dict[str, Any], request_config: dict[str, Any]) -> dict[str, str]:
        merged: dict[str, str] = {}
        for key in ("protocol", "base_url", "api_key"):
            request_value = request_config.get(key)
            saved_value = saved_config.get(key)
            if isinstance(request_value, str) and request_value.strip():
                merged[key] = request_value.strip() if key == "api_key" else request_value
            elif isinstance(saved_value, str) and saved_value.strip():
                merged[key] = saved_value.strip() if key == "api_key" else saved_value
            else:
                merged[key] = ""
        return merged

    async def _request_openai_compatible(self, base_url: str, model: str, key: str, secrets: list[str]) -> tuple[str, str]:
        encoded_model = quote(model, safe="")
        auth_headers = {"Authorization": f"Bearer {key}"}
        async with httpx.AsyncClient() as client:
            model_response = await client.get(
                f"{base_url}/models/{encoded_model}",
                headers=auth_headers,
                timeout=10.0,
            )
            if model_response.status_code != 404:
                return self._map_response(model_response, secrets)

            list_response = await client.get(
                f"{base_url}/models",
                headers=auth_headers,
                timeout=10.0,
            )
            if list_response.status_code == 200:
                try:
                    data = list_response.json().get("data")
                except Exception:
                    data = None
                if isinstance(data, list) and any(isinstance(item, dict) and item.get("id") == model for item in data):
                    return "connected", "Connection verified."
            elif list_response.status_code in (401, 403):
                return self._map_response(list_response, secrets)

            chat_response = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                timeout=10.0,
            )
            if chat_response.status_code == 400 and self._is_model_not_found_response(chat_response):
                return "model_not_found", _sanitize_message(chat_response.text or "Model not found.", secrets)
            return self._map_response(chat_response, secrets)

    def _is_model_not_found_response(self, response: httpx.Response) -> bool:
        signals = ("model_not_found", "model_not_found_error", "model not found", "model does not exist")
        haystacks = [response.text or ""]
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                for key in ("code", "type", "message"):
                    value = error.get(key)
                    if isinstance(value, str):
                        haystacks.append(value)
            for key in ("code", "type", "message"):
                value = payload.get(key)
                if isinstance(value, str):
                    haystacks.append(value)
        return any(signal in haystack.lower() for haystack in haystacks for signal in signals)

    def _map_response(self, response: httpx.Response, secrets: list[str]) -> tuple[str, str]:
        if response.status_code == 200:
            return "connected", "Connection verified."
        message = _sanitize_message(response.text or f"Provider returned status {response.status_code}.", secrets)
        if response.status_code in (401, 403):
            return "authentication_failed", message
        if response.status_code == 404:
            return "model_not_found", message
        if response.status_code in (408, 429, 500, 502, 503, 504):
            return "provider_unreachable", message
        return "unknown_error", message

    def _result(
        self,
        ok: bool,
        status: str,
        runtime: str,
        provider: str,
        model: str,
        resolved_model: str,
        message: str,
    ) -> dict[str, Any]:
        return {
            "ok": ok,
            "status": status,
            "runtime": runtime,
            "provider": provider,
            "model": model,
            "resolved_model": resolved_model,
            "message": message,
        }


provider_connection_test_service = ProviderConnectionTestService()
