"""Shared async upstream clients (one per capability category)."""
from __future__ import annotations

import httpx

from .config import GatewayConfig

_CLIENTS: dict[str, httpx.AsyncClient] = {}


def get_client(cfg: GatewayConfig, category: str) -> httpx.AsyncClient:
    """Return the shared httpx client for an upstream category.

    base_url must be the OpenAI-style root (including /v1), e.g.
    http://192.168.31.198:20003/v1 — the client's /v1 prefix is stripped
    before the path is appended.
    """
    client = _CLIENTS.get(category)
    if client is None:
        up = cfg.upstreams[category]
        headers = {"Authorization": f"Bearer {up.api_key}"} if up.api_key else {}
        client = httpx.AsyncClient(
            base_url=up.base_url,
            headers=headers,
            timeout=httpx.Timeout(up.timeout, connect=10.0),
        )
        _CLIENTS[category] = client
    return client


async def close_all() -> None:
    for client in _CLIENTS.values():
        await client.aclose()
    _CLIENTS.clear()