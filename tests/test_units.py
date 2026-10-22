from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from aigw import dlp, policy
from aigw.limits import LocalBucket, RateLimiter
from aigw.policy import ProviderSpec
from aigw.providers import (
    AnthropicProvider,
    Breaker,
    ClientError,
    Message,
    MockProvider,
    OpenAIProvider,
    ProviderError,
    Router,
)

POLICY = Path(__file__).parent.parent / "policies" / "fintech-example.yaml"


def kinds(text: str) -> list[str]:
    return [f.kind for f in dlp.scan(text)]


def test_ibans_need_the_right_length_and_check_digits():
    assert kinds("IBAN DE89 3704 0044 0532 0130 00") == ["iban"]
    assert kinds("IBAN DE89370400440532013000") == ["iban"]
    assert kinds("GB82 WEST 1234 5698 7654 32") == ["iban"]
    assert kinds("IBAN DE88 3704 0044 0532 0130 00") == []  # wrong check digits
    assert kinds("ISIN DE0007164600") == []  # an ISIN is not an IBAN


def test_cards_need_luhn_and_an_issuer_and_identifiers_are_not_cards():
    assert kinds("card 4111 1111 1111 1111") == ["card"]
    assert kinds("card 4111 1111 1111 1112") == []  # fails Luhn
    assert kinds("order 4500123456789012") == []  # Luhn-valid by chance, labelled as an order number
    assert kinds("amex 378282246310005") == ["card"]


def test_secrets_win_over_other_detectors_and_names_are_out_of_scope():
    assert kinds("key AKIAIOSFODNN7EXAMPLE") == ["secret"]
    assert kinds("-----BEGIN RSA PRIVATE KEY-----") == ["secret"]
    assert kinds("token sk-ant-api03-abcdefghijklmnopqrstuvwxyz") == ["secret"]
    assert kinds("Anna Schmidt from Berlin") == []


def test_pseudonymisation_is_consistent_and_reversible():
    text = "pay DE89 3704 0044 0532 0130 00, again DE89 3704 0044 0532 0130 00, mail a@b.de"
    p = dlp.Pseudonymiser()
    red = p.redact(text, dlp.scan(text))
    assert red == "pay [IBAN_1], again [IBAN_1], mail [EMAIL_1]"
    assert p.restore(red) == text
    s = dlp.StreamRestorer(p)
    parts = ["to [IB", "AN_1", "] and [", "EMAIL_1]!"]
    assert "".join(s.feed(x) for x in parts) + s.flush() == "to DE89 3704 0044 0532 0130 00 and a@b.de!"


def test_the_example_policy_loads_and_decides():
    loaded = policy.load(POLICY)
    eng = loaded.config.policies["engineering"]
    d = policy.decide(eng, "claude-sonnet-5-5", ["hi DE89 3704 0044 0532 0130 00"], [], 99999, None)
    assert d.allowed and d.max_tokens == 8192 and d.data_classes == {"financial"} and d.findings[0]
    marketing = loaded.config.policies["marketing"]
    d = policy.decide(marketing, "claude-haiku-5-5", ["write to a@b.de"], [], None, None)
    assert not d.allowed and d.code == "data_blocked"
    d = policy.decide(eng, "claude-sonnet-5-5", ["x"], ["create_pull_request"], None, None)
    assert d.code == "approval_required"


def test_a_policy_cannot_allow_secrets(tmp_path):
    bad = POLICY.read_text().replace(
        "data: {personal: redact, financial: redact, secret: block}",
        "data: {personal: redact, financial: redact, secret: allow}",
        1,
    )
    (tmp_path / "p.yaml").write_text(bad)
    with pytest.raises(ValueError, match="secrets can only be blocked"):
        policy.load(tmp_path / "p.yaml")


async def test_anthropic_adapter_speaks_the_messages_api():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["headers"] = request.headers
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "hello"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 12, "output_tokens": 3},
            },
        )

    p = AnthropicProvider(
        "a", ProviderSpec(kind="anthropic", base_url="https://x"), httpx.MockTransport(handler)
    )
    c = await p.complete("claude-sonnet-5-5", [Message("system", "be brief"), Message("user", "hi")], 50)
    assert (c.text, c.input_tokens, c.output_tokens) == ("hello", 12, 3)
    assert seen["body"]["system"] == "be brief" and seen["body"]["messages"] == [
        {"role": "user", "content": "hi"}
    ]
    assert seen["headers"]["anthropic-version"] == "2023-06-01"


async def test_anthropic_streaming_and_error_classes():
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 9}}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hel"}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
    ]
    sse = "".join(f"event: x\ndata: {json.dumps(e)}\n\n" for e in events)
    ok = AnthropicProvider(
        "a",
        ProviderSpec(kind="anthropic", base_url="https://x"),
        httpx.MockTransport(lambda r: httpx.Response(200, text=sse)),
    )
    parts = [p async for p in ok.stream("m", [Message("user", "hi")], 10)]
    assert parts[:2] == ["hel", "lo"] and parts[-1].input_tokens == 9 and parts[-1].output_tokens == 2
    for status, exc in ((529, ProviderError), (429, ProviderError), (400, ClientError)):
        bad = AnthropicProvider(
            "a",
            ProviderSpec(kind="anthropic", base_url="https://x"),
            httpx.MockTransport(lambda r, s=status: httpx.Response(s, text="no")),
        )
        with pytest.raises(exc):
            await bad.complete("m", [Message("user", "hi")], 10)


async def test_openai_adapter():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            },
        )

    p = OpenAIProvider("o", ProviderSpec(kind="openai", base_url="https://x"), httpx.MockTransport(handler))
    c = await p.complete("m", [Message("user", "hi")], 10)
    assert (c.text, c.input_tokens) == ("ok", 5)


def test_breaker_opens_and_probes_after_cooldown():
    b = Breaker(threshold=3, cooldown_s=10)
    for _ in range(3):
        b.failure(0)
    assert b.state == "open" and not b.available(5)
    assert b.available(11) and b.state == "half-open"
    assert not b.available(11)  # one probe at a time
    b.failure(12)
    assert b.state == "open"
    assert b.available(23)
    b.success()
    assert b.state == "closed"


async def test_router_falls_over_before_streaming_starts():
    primary, backup = MockProvider("p", failure_rate=1.0), MockProvider("b")
    router = Router({"p": primary, "b": backup})
    c, fallback = await router.complete(["p", "b"], {}, "m", [Message("user", "hi")], 10)
    assert fallback and c.provider == "b"
    parts = [x async for x in router.stream(["p", "b"], {}, "m", [Message("user", "hi")], 10)]
    assert parts[-1].provider == "b"


class DeadRedis:
    def register_script(self, _):
        async def run(keys, args):
            raise ConnectionError("redis down")

        return run


async def test_rate_limits_degrade_to_a_local_bucket_when_redis_is_down():
    limiter = RateLimiter(DeadRedis(), replicas=2)
    results = [await limiter.take("k", rpm=10, tpm=10_000, tokens=10) for _ in range(8)]
    assert all(r.degraded for r in results)
    assert sum(r.allowed for r in results) == 5  # half the rate, as one of two replicas


def test_local_bucket_refills():
    b = LocalBucket()
    assert b.take("k", 60, 60, 0)[0]
    assert not b.take("k", 60, 1, 0)[0]
    assert b.take("k", 60, 1, 1.0)[0]  # one per second
