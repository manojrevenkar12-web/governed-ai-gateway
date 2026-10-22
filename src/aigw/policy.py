"""Policy as code: which team may use which model, with what data, which tools, at what rate.

Policies live in a YAML file in the repository, so every change is reviewed in a pull request and
covered by tests (`tests/test_policy.py`). The gateway reloads the file on demand and audits each
reload with the file's hash.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from aigw import dlp

Action = Literal["allow", "redact", "block"]


class Rate(BaseModel):
    requests_per_minute: int = 60
    tokens_per_minute: int = 100_000


class Policy(BaseModel):
    description: str = ""
    models: list[str]
    data: dict[str, Action] = Field(
        default_factory=lambda: {"personal": "redact", "financial": "redact", "secret": "block"}
    )
    max_output_tokens: int = 4096
    tools: list[str] = Field(default_factory=list)
    tools_requiring_approval: list[str] = Field(default_factory=list)
    rate: Rate = Field(default_factory=Rate)


class ModelSpec(BaseModel):
    providers: list[str]  # in order of preference; later ones are fallbacks
    upstream_model: dict[str, str] = Field(default_factory=dict)  # provider -> its name for the model
    price_in: float  # USD per million input tokens
    price_out: float  # USD per million output tokens
    context_tokens: int = 200_000


class ProviderSpec(BaseModel):
    kind: Literal["anthropic", "openai", "mock"]
    base_url: str = ""
    api_key_env: str = ""
    timeout_s: float = 60.0
    options: dict = Field(default_factory=dict)


class Config(BaseModel):
    providers: dict[str, ProviderSpec]
    models: dict[str, ModelSpec]
    policies: dict[str, Policy]


@dataclass
class Loaded:
    config: Config
    sha256: str
    path: str


def load(path: str | Path) -> Loaded:
    raw = Path(path).read_bytes()
    config = Config.model_validate(yaml.safe_load(raw))
    for name, p in config.policies.items():
        unknown = [m for m in p.models if m not in config.models]
        if unknown:
            raise ValueError(f"policy {name} allows unknown models {unknown}")
        bad = [c for c in p.data if c not in set(dlp.CLASSES.values())]
        if bad:
            raise ValueError(f"policy {name} names unknown data classes {bad}")
        if p.data.get("secret", "block") != "block":
            raise ValueError(f"policy {name}: secrets can only be blocked")
    for name, m in config.models.items():
        missing = [p for p in m.providers if p not in config.providers]
        if missing:
            raise ValueError(f"model {name} uses unknown providers {missing}")
    return Loaded(config, hashlib.sha256(raw).hexdigest(), str(path))


@dataclass
class Decision:
    allowed: bool
    status: int = 200
    code: str = "ok"
    reason: str = ""
    findings: dict[int, list[dlp.Finding]] = field(default_factory=dict)  # message index -> to redact
    data_classes: set[str] = field(default_factory=set)
    max_tokens: int = 0


def decide(
    policy: Policy,
    model: str,
    texts: list[str],
    tools: list[str],
    max_tokens: int | None,
    approval: str | None,
) -> Decision:
    """Pure function: same inputs, same decision. Values found by DLP never appear in a reason."""
    if model not in policy.models:
        return Decision(False, 403, "model_not_allowed", f"model {model} is not approved for this team")
    for tool in tools:
        if tool not in policy.tools:
            return Decision(False, 403, "tool_not_allowed", f"tool {tool} is not approved for this team")
        if tool in policy.tools_requiring_approval and not approval:
            return Decision(
                False,
                403,
                "approval_required",
                f"tool {tool} needs an approval reference (X-Approval header)",
            )
    findings: dict[int, list[dlp.Finding]] = {}
    classes: set[str] = set()
    blocked: dict[str, int] = {}
    for i, text in enumerate(texts):
        fs = dlp.scan(text)
        keep = []
        for f in fs:
            classes.add(f.data_class)
            action = policy.data.get(f.data_class, "block")
            if action == "block":
                blocked[f.kind] = blocked.get(f.kind, 0) + 1
            elif action == "redact":
                keep.append(f)
        if keep:
            findings[i] = keep
    if blocked:
        what = ", ".join(f"{n} {k}" for k, n in sorted(blocked.items()))
        return Decision(
            False,
            403,
            "data_blocked",
            f"request contains data this team may not send: {what}",
            data_classes=classes,
        )
    limit = policy.max_output_tokens
    return Decision(True, findings=findings, data_classes=classes, max_tokens=min(max_tokens or limit, limit))
