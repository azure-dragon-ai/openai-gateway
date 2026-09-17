"""Gateway configuration.

Each capability (chat / image / video) routes to its own upstream with an
independent base_url, api_key, timeout and client->upstream model mapping.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class UpstreamConfig:
    base_url: str
    api_key: str = ""
    timeout: float = 300.0
    models: dict[str, str] = field(default_factory=dict)

    def resolve_model(self, client_model: str) -> str:
        """Map a client-facing model name to the upstream model name.

        Unknown client names pass through unchanged (the upstream may know
        more models than the mapping lists).
        """
        return self.models.get(client_model, client_model)

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
    chat: UpstreamConfig
    image: UpstreamConfig
    video: UpstreamConfig

    @property
    def upstreams(self) -> dict[str, UpstreamConfig]:
        return {"chat": self.chat, "image": self.image, "video": self.video}


def _parse_upstream(name: str, raw: dict) -> UpstreamConfig:
    if not isinstance(raw, dict) or "base_url" not in raw:
        raise ValueError(f"upstream section '{name}' missing required field: base_url")
    return UpstreamConfig(
        base_url=str(raw["base_url"]).rstrip("/"),
        api_key=str(raw.get("api_key", "")),
        timeout=float(raw.get("timeout", 300.0)),
        models={str(k): str(v) for k, v in (raw.get("models") or {}).items()},
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

    missing = [k for k in ("chat", "image", "video") if k not in raw]
    if missing:
        raise ValueError(f"config missing upstream sections: {missing}")

    return GatewayConfig(
        server=server,
        chat=_parse_upstream("chat", raw["chat"]),
        image=_parse_upstream("image", raw["image"]),
        video=_parse_upstream("video", raw["video"]),
    )