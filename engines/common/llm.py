"""Provider adapters for the reasoning engines; credentials never enter traces."""
import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

import httpx
from dotenv import load_dotenv

load_dotenv()


@dataclass
class LLMResult:
    text: str
    usage: dict = field(default_factory=dict)


class LLMClient(Protocol):
    async def generate(self, messages: list[dict], *, json_schema: dict | None = None,
                       max_tokens: int = 1800, timeout: float = 60) -> LLMResult: ...


class ProviderError(RuntimeError):
    pass


class ScriptedLLM:
    """Explicitly supplied model outputs for tests and recorded replay only."""

    def __init__(self, responses: list[str | LLMResult | Exception]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def generate(self, messages: list[dict], *, json_schema: dict | None = None,
                       max_tokens: int = 1800, timeout: float = 60) -> LLMResult:
        self.calls.append({"messages": messages, "json_schema": json_schema,
                           "max_tokens": max_tokens, "timeout": timeout})
        if not self.responses:
            raise ProviderError("У ScriptedLLM закончились ответы")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, str):
            response = LLMResult(response)
        if not response.text.strip():
            raise ProviderError("Провайдер вернул пустой ответ")
        return response


class OpenAICompatibleLLM:
    def __init__(self, *, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    async def generate(self, messages: list[dict], *, json_schema: dict | None = None,
                       max_tokens: int = 1800, timeout: float = 60) -> LLMResult:
        if not self.api_key:
            raise ProviderError("Отсутствует ключ API провайдера")
        body: dict[str, Any] = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
        if json_schema is not None:
            body["response_format"] = {"type": "json_object"}
            body["messages"] = [{"role": "system", "content": "Верни только корректный JSON-объект."}, *messages]
        verify = os.getenv("OPENAI_CA_BUNDLE") or True
        async with httpx.AsyncClient(timeout=timeout, verify=verify) as client:
            for attempt in range(3):
                try:
                    response = await client.post(
                        f"{self.base_url}/chat/completions", json=body,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    )
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                        await asyncio.sleep(0.4 * (2 ** attempt))
                        continue
                    response.raise_for_status()
                    payload = response.json()
                    choice = payload["choices"][0]
                    if choice.get("finish_reason") == "length" or choice["message"].get("refusal"):
                        raise ProviderError("Ответ модели усечён или отклонён")
                    text = choice["message"]["content"]
                    if not isinstance(text, str) or not text.strip():
                        raise ProviderError("Провайдер вернул пустой ответ")
                    return LLMResult(text=text, usage=payload.get("usage") or {})
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    if attempt == 2:
                        raise ProviderError(f"Сбой сети провайдера: {type(exc).__name__}") from exc
                    await asyncio.sleep(0.4 * (2 ** attempt))
                except (httpx.HTTPStatusError, KeyError, IndexError, TypeError, ValueError) as exc:
                    raise ProviderError(f"Некорректный ответ провайдера: {type(exc).__name__}") from exc
        raise ProviderError("Провайдер недоступен")


class GigaChatLLM:
    def __init__(self, *, authorization_key: str, model: str,
                 oauth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
                 base_url: str = "https://gigachat.devices.sberbank.ru/api/v1"):
        self.authorization_key = authorization_key
        self.model = model
        self.oauth_url = oauth_url
        self.base_url = base_url.rstrip("/")
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        async with self._lock:
            if self._token and time.time() < self._expires_at - 60:
                return self._token
            if not self.authorization_key:
                raise ProviderError("Отсутствует ключ авторизации GigaChat")
            try:
                response = await client.post(
                    self.oauth_url, data={"scope": os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")},
                    headers={"Authorization": f"Basic {self.authorization_key}",
                             "RqUID": str(uuid4()), "Content-Type": "application/x-www-form-urlencoded"},
                )
                response.raise_for_status()
                data = response.json()
                self._token = data["access_token"]
                expiry = float(data.get("expires_at", 0))
                self._expires_at = expiry / 1000 if expiry > 10**11 else expiry or time.time() + 1800
                return self._token
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                raise ProviderError(f"Не удалось получить токен GigaChat: {type(exc).__name__}") from exc

    async def generate(self, messages: list[dict], *, json_schema: dict | None = None,
                       max_tokens: int = 1800, timeout: float = 60) -> LLMResult:
        verify = os.getenv("GIGACHAT_CA_BUNDLE") or True
        async with httpx.AsyncClient(timeout=timeout, verify=verify) as client:
            token = await self._access_token(client)
            body: dict[str, Any] = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
            if json_schema is not None:
                body["messages"] = [{"role": "system", "content": "Верни только корректный JSON-объект."}, *messages]
            for attempt in range(3):
                try:
                    response = await client.post(
                        f"{self.base_url}/chat/completions", json=body,
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                        await asyncio.sleep(0.4 * (2 ** attempt))
                        continue
                    response.raise_for_status()
                    data = response.json()
                    answer = data["choices"][0]["message"]["content"]
                    if not isinstance(answer, str) or not answer.strip():
                        raise ProviderError("GigaChat вернул пустой ответ")
                    return LLMResult(answer, data.get("usage") or {})
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    if attempt == 2:
                        raise ProviderError(f"Сбой сети GigaChat: {type(exc).__name__}") from exc
                    await asyncio.sleep(0.4 * (2 ** attempt))
                except (httpx.HTTPStatusError, KeyError, IndexError, TypeError, ValueError) as exc:
                    raise ProviderError(f"Некорректный ответ GigaChat: {type(exc).__name__}") from exc
        raise ProviderError("GigaChat недоступен")


class FallbackLLM:
    def __init__(self, clients: list[LLMClient]):
        if not clients:
            raise ValueError("Нужен хотя бы один провайдер")
        self.clients = clients

    async def generate(self, messages: list[dict], *, json_schema: dict | None = None,
                       max_tokens: int = 1800, timeout: float = 60) -> LLMResult:
        errors = []
        for client in self.clients:
            try:
                return await client.generate(messages, json_schema=json_schema,
                                             max_tokens=max_tokens, timeout=timeout)
            except ProviderError as exc:
                errors.append(str(exc))
        raise ProviderError("Все провайдеры недоступны: " + "; ".join(errors))


def _make(role: str) -> tuple[LLMClient, tuple[str, str]]:
    provider = os.getenv(f"{role}_PROVIDER", "openai").lower()
    model = os.getenv(f"{role}_MODEL") or ("gpt-4.1-mini" if role == "ACTOR" else "gpt-4.1-nano")
    if provider == "gigachat":
        return GigaChatLLM(authorization_key=os.getenv("GIGACHAT_AUTH_KEY", ""), model=model), (provider, model)
    if provider in {"openai", "nvidia", "custom"}:
        prefix = "OPENAI" if provider == "openai" else provider.upper()
        base = os.getenv(f"{prefix}_BASE_URL", "https://api.openai.com/v1")
        return OpenAICompatibleLLM(base_url=base, api_key=os.getenv(f"{prefix}_API_KEY", ""), model=model), (provider, model)
    raise ValueError(f"Неизвестный провайдер: {provider}")


def build_role_clients() -> tuple[LLMClient, LLMClient]:
    actor, actor_id = _make("ACTOR")
    judge, judge_id = _make("JUDGE")
    if actor_id == judge_id:
        raise ValueError("Актор и судья должны использовать разные модели")
    return actor, judge


def provider_health() -> dict:
    roles = {}
    for role in ("ACTOR", "JUDGE"):
        provider = os.getenv(f"{role}_PROVIDER", "openai").lower()
        prefix = "OPENAI" if provider == "openai" else provider.upper()
        variable = "GIGACHAT_AUTH_KEY" if provider == "gigachat" else f"{prefix}_API_KEY"
        roles[role.lower()] = {"provider": provider, "configured": bool(os.getenv(variable)),
                               "model": os.getenv(f"{role}_MODEL")}
    return {"roles": roles, "check": "configuration_only"}
