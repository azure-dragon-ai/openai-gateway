"""Gateway configuration.

Each capability (chat / image / video / audio) routes to its own upstream with
an independent base_url, api_key, timeout and client->upstream model mapping.

Categories are data-driven: any top-level section that carries a `base_url` is
treated as an upstream. `chat`, `image` and `video` are required (historical
sections); `audio` (TTS) and any other custom section are optional.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("openai-gateway")

#: Sections that are always parsed as upstreams.
REQUIRED_CATEGORIES = ("chat", "image", "video")

#: Sections that are parsed when present, but not required.
OPTIONAL_CATEGORIES = ("audio",)

#: Top-level keys that are never treated as an upstream.
_RESERVED_SECTIONS = {"server"}


@dataclass
class UpstreamConfig:
    base_url: str
    api_key: str = ""
    timeout: float = 300.0
    models: dict[str, str] = field(default_factory=dict)
    serialize: bool = False
    #: Any yaml key beyond the ones above. Lets a category carry its own
    #: adapter settings (e.g. audio's voice whitelists) without touching this
    #: parser every time.
    extras: dict[str, Any] = field(default_factory=dict)

    def resolve_model(self, client_model: str) -> str:
        """Map a client-facing model name to the upstream model name.

        Unknown client names pass through unchanged (the upstream may know
        more models than the mapping lists).
        """
        return self.models.get(client_model, client_model)

    def knows(self, client_model: str) -> bool:
        """True if this upstream explicitly declares the client-facing name."""
        return client_model in self.models

    def to_client_name(self, upstream_model: str) -> str:
        """Reverse-map an upstream model name back to the client-facing name."""
        for client, upstream in self.models.items():
            if upstream == upstream_model:
                return client
        return upstream_model


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8100
    api_key: str = ""


@dataclass
class GatewayConfig:
    server: ServerConfig
    upstreams: dict[str, UpstreamConfig]

    # -- convenience accessors kept for readability at call sites -----------
    @property
    def chat(self) -> UpstreamConfig:
        return self.upstreams["chat"]

    @property
    def image(self) -> UpstreamConfig:
        return self.upstreams["image"]

    @property
    def video(self) -> UpstreamConfig:
        return self.upstreams["video"]

    @property
    def audio(self) -> UpstreamConfig | None:
        return self.upstreams.get("audio")

    def category_for_model(self, model: str) -> str | None:
        """Return the category that declares `model`, or None if unknown."""
        for name, up in self.upstreams.items():
            if up.knows(model):
                return name
        return None


#: Upstream keys mapped onto UpstreamConfig fields; everything else in the
#: section lands in `extras`.
_CORE_UPSTREAM_KEYS = {"base_url", "api_key", "timeout", "models", "serialize"}


def _parse_upstream(name: str, raw: dict) -> UpstreamConfig:
    if not isinstance(raw, dict) or "base_url" not in raw:
        raise ValueError(f"upstream section '{name}' missing required field: base_url")
    return UpstreamConfig(
        base_url=str(raw["base_url"]).rstrip("/"),
        api_key=str(raw.get("api_key", "")),
        timeout=float(raw.get("timeout", 300.0)),
        models={str(k): str(v) for k, v in (raw.get("models") or {}).items()},
        serialize=bool(raw.get("serialize", False)),
        extras={k: v for k, v in raw.items() if k not in _CORE_UPSTREAM_KEYS},
    )


def load_config(path: str | Path) -> GatewayConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"invalid config file: {path}")

    server_raw = raw.get("server") or {}
    server = ServerConfig(
        host=str(server_raw.get("host", "0.0.0.0")),
        port=int(server_raw.get("port", 8100)),
        api_key=str(server_raw.get("api_key", "")),
    )

    # Known categories first, so their ordering in /v1/models is stable.
    categories: list[str] = [
        c for c in (*REQUIRED_CATEGORIES, *OPTIONAL_CATEGORIES) if c in raw
    ]
    missing = [c for c in REQUIRED_CATEGORIES if c not in raw]
    if missing:
        raise ValueError(f"config missing upstream sections: {missing}")

    # Any other section carrying a base_url becomes an extra category.
    for key, value in raw.items():
        if key in _RESERVED_SECTIONS or key in categories:
            continue
        if isinstance(value, dict) and "base_url" in value:
            categories.append(key)
            log.info("config: discovered extra upstream category '%s'", key)

    upstreams = {name: _parse_upstream(name, raw[name]) for name in categories}
    return GatewayConfig(server=server, upstreams=upstreams)
