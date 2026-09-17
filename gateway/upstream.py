"""Shared async upstream clients (one per capability category)."""
from __future__ import annotations

import httpx

from .config import GatewayConfig

_CLIENTS: dict[str, httpx.AsyncClient] = {}


def get_client(cfg: GatewayConfig, category: str) -> httpx.AsyncClient:
    """Return the shared httpx client for an upstream category.

    base_url must be the service root WITHOUT /v1, e.g.
    http://192.168.31.198:20003 — the gateway forwards the client's
    /v1 paths verbatim onto this root.
    """
    client = _CLIENTS.get(category)
    if client is None:
        up = cfg.upstreams[category]
        headers = {"Authorization": f"Bearer {up.api_key}"} if up.api_key else {}
        # Store the raw root URL (no /v1 merging): the gateway builds full
        # OpenAI-style paths like /v1/images/generations itself.
        client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(up.timeout, connect=10.0),
        )
        client._raw_base = up.base_url  # e.g. http://host:port/v1
        _CLIENTS[category] = client
    return client


async def close_all() -> None:
    for client in _CLIENTS.values():
        await client.aclose()
    _CLIENTS.clear()