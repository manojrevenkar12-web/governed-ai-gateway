"""Model providers behind the gateway, and the routing between them.

Clients speak one API (OpenAI-compatible chat completions); providers are adapters. A model can
have several providers in order of preference. A provider that keeps failing is taken out of
rotation by a circuit breaker and probed again after a cool-down; a request falls over to the next
provider as long as nothing has been streamed to the client yet.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

from aigw.policy import ProviderSpec


@dataclass
class Message:
    role: str
    content: str


@dataclass
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    finish_reason: str = "stop"
    provider: str = ""


class ProviderError(Exception):
    """A failure worth trying another provider for: 5xx, 429, timeouts, connection errors."""


class ClientError(Exception):
    """The request itself is wrong (4xx other than 429): another provider will not help."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


class Provider:
    name: str

    async def complete(self, model: str, messages: list[Message], max_tokens: int) -> Completion:
        raise NotImplementedError

    async def stream(
        self, model: str, messages: list[Message], max_tokens: int
    ) -> AsyncIterator[str | Completion]:
        """Yields text chunks, then a final Completion with usage."""
        c = await self.complete(model, messages, max_tokens)
        yield c.text
        yield c


# ---- adapters -------------------------------------------------------------------------------------------


class AnthropicProvider(Provider):
    """Anthropic Messages API."""

    def __init__(self, name: str, spec: ProviderSpec, transport: httpx.AsyncBaseTransport | None = None):
        self.name = name
        self.http = httpx.AsyncClient(
            base_url=spec.base_url,
            timeout=spec.timeout_s,
            transport=transport,
            headers={"x-api-key": os.environ.get(spec.api_key_env, ""), "anthropic-version": "2023-06-01"},
        )

    @staticmethod
    def _body(model: str, messages: list[Message], max_tokens: int, stream: bool) -> dict:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "stream": stream,
            "messages": [{"role": m.role, "content": m.content} for m in messages if m.role != "system"],
        }
        if system:
            body["system"] = system
        return body

    async def _post(self, body: dict) -> httpx.Response:
        try:
            r = await self.http.post("/v1/messages", json=body)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise ProviderError(f"{self.name}: {type(exc).__name__}") from exc
        if r.status_code == 429 or r.status_code >= 500:
            raise ProviderError(f"{self.name}: HTTP {r.status_code}")
        if r.status_code >= 400:
            raise ClientError(r.status_code, r.text[:500])
        return r

    async def complete(self, model: str, messages: list[Message], max_tokens: int) -> Completion:
        r = await self._post(self._body(model, messages, max_tokens, False))
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage", {})
        return Completion(
            text,
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            "length" if data.get("stop_reason") == "max_tokens" else "stop",
            self.name,
        )

    async def stream(
        self, model: str, messages: list[Message], max_tokens: int
    ) -> AsyncIterator[str | Completion]:
        body = self._body(model, messages, max_tokens, True)
        try:
            async with self.http.stream("POST", "/v1/messages", json=body) as r:
                if r.status_code == 429 or r.status_code >= 500:
                    raise ProviderError(f"{self.name}: HTTP {r.status_code}")
                if r.status_code >= 400:
                    raise ClientError(r.status_code, (await r.aread()).decode()[:500])
                tokens_in = tokens_out = 0
                finish = "stop"
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    event = json.loads(line[5:])
                    kind = event.get("type")
                    if kind == "message_start":
                        tokens_in = event["message"]["usage"].get("input_tokens", 0)
                    elif kind == "content_block_delta" and event["delta"].get("type") == "text_delta":
                        yield event["delta"]["text"]
                    elif kind == "message_delta":
                        tokens_out = event.get("usage", {}).get("output_tokens", tokens_out)
                        if event.get("delta", {}).get("stop_reason") == "max_tokens":
                            finish = "length"
                yield Completion("", tokens_in, tokens_out, finish, self.name)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise ProviderError(f"{self.name}: {type(exc).__name__}") from exc


class OpenAIProvider(Provider):
    """OpenAI-compatible chat completions (OpenAI, Azure OpenAI, vLLM and most self-hosted servers)."""

    def __init__(self, name: str, spec: ProviderSpec, transport: httpx.AsyncBaseTransport | None = None):
        self.name = name
        self.http = httpx.AsyncClient(
            base_url=spec.base_url,
            timeout=spec.timeout_s,
            transport=transport,
            headers={"Authorization": f"Bearer {os.environ.get(spec.api_key_env, '')}"},
        )

    async def complete(self, model: str, messages: list[Message], max_tokens: int) -> Completion:
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        try:
            r = await self.http.post("/v1/chat/completions", json=body)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise ProviderError(f"{self.name}: {type(exc).__name__}") from exc
        if r.status_code == 429 or r.status_code >= 500:
            raise ProviderError(f"{self.name}: HTTP {r.status_code}")
        if r.status_code >= 400:
            raise ClientError(r.status_code, r.text[:500])
        data = r.json()
        choice = data["choices"][0]
        usage = data.get("usage", {})
        return Completion(
            choice["message"].get("content") or "",
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            choice.get("finish_reason", "stop"),
            self.name,
        )


@dataclass
class MockProvider(Provider):
    """A stand-in model for tests, load tests and demos. It answers with what it was sent, so tests
    can prove what left the company, and it can be made slow or unreliable."""

    name: str = "mock"
    latency_ms: float = 0.0
    failure_rate: float = 0.0
    seed: int = 0
    calls: int = 0
    seen: list[list[Message]] = field(default_factory=list)
    keep_seen: bool = True

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)

    async def complete(self, model: str, messages: list[Message], max_tokens: int) -> Completion:
        self.calls += 1
        if self.keep_seen:
            self.seen.append(messages)
        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000)
        if self.rng.random() < self.failure_rate:
            raise ProviderError(f"{self.name}: injected failure")
        prompt = "\n".join(m.content for m in messages)
        text = f"Echo: {messages[-1].content}"[: max_tokens * 4]
        return Completion(text, estimate_tokens(prompt), estimate_tokens(text), "stop", self.name)

    async def stream(
        self, model: str, messages: list[Message], max_tokens: int
    ) -> AsyncIterator[str | Completion]:
        c = await self.complete(model, messages, max_tokens)
        for i in range(0, len(c.text), 7):  # small chunks, so placeholders get split across them
            yield c.text[i : i + 7]
        yield Completion("", c.input_tokens, c.output_tokens, c.finish_reason, self.name)


def build(name: str, spec: ProviderSpec) -> Provider:
    if spec.kind == "anthropic":
        return AnthropicProvider(name, spec)
    if spec.kind == "openai":
        return OpenAIProvider(name, spec)
    return MockProvider(name, **spec.options)


# ---- routing -----------------------------------------------------------------------------------------------


@dataclass
class Breaker:
    """Opens after `threshold` consecutive failures; after `cooldown_s` lets one probe through."""

    threshold: int = 5
    cooldown_s: float = 30.0
    failures: int = 0
    opened_at: float | None = None
    probing: bool = False

    def available(self, now: float) -> bool:
        if self.opened_at is None:
            return True
        if now - self.opened_at >= self.cooldown_s and not self.probing:
            self.probing = True
            return True
        return False

    def success(self) -> None:
        self.failures, self.opened_at, self.probing = 0, None, False

    def failure(self, now: float) -> None:
        self.failures += 1
        if self.probing or self.failures >= self.threshold:
            self.opened_at, self.probing = now, False

    @property
    def state(self) -> str:
        return "closed" if self.opened_at is None else ("half-open" if self.probing else "open")


class NoProvider(Exception):
    pass


class Router:
    def __init__(self, providers: dict[str, Provider], breaker_threshold: int = 5, cooldown_s: float = 30.0):
        self.providers = providers
        self.breakers = {n: Breaker(breaker_threshold, cooldown_s) for n in providers}

    def candidates(self, names: list[str]) -> list[str]:
        now = time.monotonic()
        return [n for n in names if n in self.providers and self.breakers[n].available(now)]

    async def complete(
        self, names: list[str], upstream: dict[str, str], model: str, messages: list[Message], max_tokens: int
    ) -> tuple[Completion, bool]:
        """Returns the completion and whether a fallback provider served it."""
        errors = []
        for i, name in enumerate(self.candidates(names)):
            try:
                c = await self.providers[name].complete(upstream.get(name, model), messages, max_tokens)
            except ProviderError as exc:
                self.breakers[name].failure(time.monotonic())
                errors.append(str(exc))
                continue
            self.breakers[name].success()
            c.provider = name
            return c, i > 0 or name != names[0]
        raise NoProvider("; ".join(errors) or "every provider for this model is unavailable")

    async def stream(
        self, names: list[str], upstream: dict[str, str], model: str, messages: list[Message], max_tokens: int
    ) -> AsyncIterator[str | Completion]:
        errors = []
        for name in self.candidates(names):
            started = False
            try:
                async for part in self.providers[name].stream(
                    upstream.get(name, model), messages, max_tokens
                ):
                    started = True
                    if isinstance(part, Completion):
                        part.provider = name
                    yield part
                self.breakers[name].success()
                return
            except ProviderError as exc:
                self.breakers[name].failure(time.monotonic())
                if started:  # the client already has part of an answer: do not splice in another
                    raise
                errors.append(str(exc))
        raise NoProvider("; ".join(errors) or "every provider for this model is unavailable")
