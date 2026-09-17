"""OpenAI-compatible gateway routes.

The gateway forwards the client's original /v1 path verbatim to the
configured upstream root (base_url is the service root WITHOUT /v1),
so every subpath is a transparent passthrough:

  GET  /health
  GET  /v1/models
  POST /v1/chat/completions           (streaming SSE + non-streaming)
  POST /v1/images/{subpath:path}      (transparent proxy, e.g. /generations)
  GET  /v1/images/{subpath:path}      (transparent proxy, e.g. /tasks/{id})
  POST /v1/videos/{subpath:path}      (transparent proxy, e.g. /generations)
  GET  /v1/videos/{subpath:path}      (transparent proxy, e.g. /tasks/{id})
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import upstream as up
from .config import GatewayConfig

log = logging.getLogger("openai-gateway")


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"error": {"message": "invalid or missing api key", "type": "auth_error"}},
        status_code=401,
    )


def _bad_request(message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": "invalid_request_error"}},
        status_code=400,
    )


def _upstream_error(resp) -> Response:
    return Response(
        status_code=resp.status_code,
        content=resp.content,
        media_type=resp.headers.get("content-type", "application/json"),
    )


def _upstream_url(base: str, request: Request) -> str:
    """Forward the client's original /v1 path to the upstream root verbatim."""
    path = request.url.path
    if not path.startswith("/v1"):
        path = f"/v1{path}"
    if request.url.query:
        path = f"{path}?{request.url.query}"
    return f"{base}{path}"


def create_app(cfg: GatewayConfig) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await up.close_all()

    app = FastAPI(title="openai-gateway", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _auth_ok(request: Request) -> bool:
        if not cfg.server.api_key:
            return True
        key = request.headers.get("x-api-key", "")
        if not key:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                key = auth[7:].strip()
        return key == cfg.server.api_key

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "upstreams": {
                name: {"base_url": c.base_url, "models": list(c.models)}
                for name, c in cfg.upstreams.items()
            },
        }

    @app.get("/v1/models")
    async def list_models(request: Request):
        if not _auth_ok(request):
            return _unauthorized()
        data = [
            {"id": model, "object": "model", "created": 0, "owned_by": category}
            for category, c in cfg.upstreams.items()
            for model in c.models
        ]
        return {"object": "list", "data": data}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        if not _auth_ok(request):
            return _unauthorized()
        body = await request.json()
        chat_cfg = cfg.chat
        model = body.get("model")
        if not model:
            return _bad_request("'model' is required")
        body["model"] = chat_cfg.resolve_model(model)

        client = up.get_client(cfg, "chat")
        target = _upstream_url(client._raw_base, request)

        if body.get("stream"):
            # Open the upstream response first so we can surface non-200
            # statuses before the streaming response is committed.
            req = client.build_request("POST", target, json=body)
            resp = await client.send(req, stream=True)
            if resp.status_code != 200:
                err = await resp.aread()
                await resp.aclose()
                return Response(
                    status_code=resp.status_code,
                    content=err,
                    media_type="application/json",
                )

            async def gen():
                try:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                finally:
                    await resp.aclose()

            return StreamingResponse(
                gen(),
                media_type=resp.headers.get("content-type", "text/event-stream"),
            )

        resp = await client.post(target, json=body)
        if resp.status_code != 200:
            return _upstream_error(resp)
        data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("model"), str):
            data["model"] = chat_cfg.to_client_name(data["model"])
        return JSONResponse(data)

    async def _proxy(request: Request, category: str, subpath: str):
        """Transparent proxy: forward the client's /v1 path verbatim."""
        if not _auth_ok(request):
            return _unauthorized()
        up_cfg = cfg.upstreams[category]
        client = up.get_client(cfg, category)
        target = _upstream_url(client._raw_base, request)

        raw = await request.body()
        body = None
        if request.method in ("POST", "PUT") and raw:
            content_type = request.headers.get("content-type", "")
            if content_type.startswith("application/json"):
                try:
                    body = json.loads(raw)
                except (ValueError, TypeError):
                    body = None
                if isinstance(body, dict) and body.get("model"):
                    body["model"] = up_cfg.resolve_model(body["model"])

        if body is not None:
            resp = await client.request(request.method, target, json=body)
        else:
            resp = await client.request(request.method, target, content=raw)

        if resp.status_code != 200:
            return _upstream_error(resp)

        ct = resp.headers.get("content-type", "")
        if ct.startswith("application/json"):
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("model"), str):
                data["model"] = up_cfg.to_client_name(data["model"])
            return JSONResponse(data)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=ct,
        )

    @app.post("/v1/images/{subpath:path}")
    async def images_post(request: Request, subpath: str):
        return await _proxy(request, "image", subpath)

    @app.get("/v1/images/{subpath:path}")
    async def images_get(request: Request, subpath: str):
        return await _proxy(request, "image", subpath)

    @app.post("/v1/videos/{subpath:path}")
    async def videos_post(request: Request, subpath: str):
        return await _proxy(request, "video", subpath)

    @app.get("/v1/videos/{subpath:path}")
    async def videos_get(request: Request, subpath: str):
        return await _proxy(request, "video", subpath)

    return app