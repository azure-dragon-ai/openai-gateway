"""OpenAI-compatible gateway routes.

The gateway forwards the client's original /v1 path verbatim to the
configured upstream root (base_url is the service root WITHOUT /v1),
so every subpath is a transparent passthrough:

  GET  /health
  GET  /v1/models
  POST /v1/chat/completions           (streaming SSE + non-streaming)
  POST /v1/videos                     (OpenAI video format: create async task)
  GET  /v1/videos/{task_id}           (OpenAI video format: task status)
  GET  /v1/videos/{task_id}/content   (OpenAI video format: proxy download)
  POST /v1/images/{subpath:path}      (transparent proxy, e.g. /generations)
  GET  /v1/images/{subpath:path}      (transparent proxy, e.g. /tasks/{id})
  POST /v1/videos/{subpath:path}      (transparent proxy, e.g. /generations)
  GET  /v1/videos/{subpath:path}      (transparent proxy, e.g. /tasks/{id})
"""
from __future__ import annotations

import json
import logging
import re
import time
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


_VIDEO_STATUS_MAP = {
    "pending": "queued",
    "queued": "queued",
    "in_progress": "in_progress",
    "incomplete": "in_progress",
    "completed": "completed",
    "failed": "failed",
    "error": "failed",
}


def _map_video_status(raw: str) -> str:
    return _VIDEO_STATUS_MAP.get(raw, raw)


def _video_progress(status: str) -> int:
    if status in ("completed", "failed"):
        return 100
    if status == "in_progress":
        return 50
    return 0


def _output_urls(data: dict) -> list:
    out = data.get("output")
    if not isinstance(out, dict):
        return []
    items = out.get("data")
    if not isinstance(items, list):
        return []
    return [it["url"] for it in items if isinstance(it, dict) and it.get("url")]


def _parse_multipart_fields(raw: bytes, content_type: str) -> dict:
    """Parse multipart/form-data into a flat dict of text fields."""
    m = re.search(r"boundary=(\"([^\"]+)\"|([^;]+))", content_type)
    if not m:
        return {}
    boundary = ("--" + (m.group(2) or m.group(3))).encode("latin-1")
    fields: dict = {}
    for part in raw.split(boundary):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        sep = part.find(b"\r\n\r\n")
        if sep == -1:
            continue
        head = part[:sep].decode("latin-1", errors="replace")
        value = part[sep + 4:]
        if value.endswith(b"\r\n"):
            value = value[:-2]
        dm = re.search(r'name="([^"]+)"', head)
        if dm:
            fields[dm.group(1)] = value.decode("utf-8", errors="replace")
    return fields


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

    @app.post("/v1/videos")
    async def videos_create(request: Request):
        """OpenAI video format: create an async video task."""
        if not _auth_ok(request):
            return _unauthorized()
        raw = await request.body()
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            try:
                body = json.loads(raw)
            except (ValueError, TypeError):
                return _bad_request("invalid JSON body")
        elif content_type.startswith("multipart/form-data"):
            body = _parse_multipart_fields(raw, content_type)
            if not body:
                return _bad_request("no form fields found")
        else:
            return _bad_request(
                "unsupported content-type (use application/json or multipart/form-data)"
            )
        if not body.get("model"):
            return _bad_request("'model' is required")
        if not body.get("prompt"):
            return _bad_request("'prompt' is required")
        up_cfg = cfg.upstreams["video"]
        body["model"] = up_cfg.resolve_model(body["model"])
        body["background"] = "pending"
        # Upstream (roundabout minimax-h3) reads `duration`, not `seconds`.
        # OpenAI-style clients send `seconds`; map it so duration is honored.
        if body.get("duration") is None and body.get("seconds") is not None:
            try:
                # Upstream minimax-h3 hard limit: 1..15 seconds; clamp above 15.
                body["duration"] = min(int(str(body["seconds"]).strip()), 15)
            except ValueError:
                return _bad_request("'seconds' must be an integer number of seconds")
        body.pop("seconds", None)
        client = up.get_client(cfg, "video")
        resp = await client.post(f"{client._raw_base}/v1/videos/generations", json=body)
        if resp.status_code != 200:
            return _upstream_error(resp)
        data = resp.json()
        status = _map_video_status(data.get("status", "pending"))
        result = {
            "id": data.get("id"),
            "task_id": data.get("id"),
            "object": "video",
            "model": up_cfg.to_client_name(body["model"]),
            "status": status,
            "progress": _video_progress(status),
            "created_at": data.get("created_at", int(time.time())),
        }
        if body.get("seconds") is not None:
            result["seconds"] = body["seconds"]
        if body.get("size") is not None:
            result["size"] = body["size"]
        return JSONResponse(result)

    @app.get("/v1/videos/{task_id}/content")
    async def videos_content(task_id: str, request: Request):
        """OpenAI video format: proxy download of the finished video."""
        if not _auth_ok(request):
            return _unauthorized()
        client = up.get_client(cfg, "video")
        resp = await client.get(f"{client._raw_base}/v1/videos/tasks/{task_id}")
        if resp.status_code != 200:
            return _upstream_error(resp)
        data = resp.json()
        status = _map_video_status(data.get("status", "pending"))
        if status != "completed":
            return JSONResponse(
                {"error": {"message": f"task not completed (status={status})",
                           "type": "invalid_request_error", "code": "task_not_completed"}},
                status_code=409,
            )
        urls = _output_urls(data)
        if not urls:
            return JSONResponse(
                {"error": {"message": "no output url in task",
                           "type": "upstream_error", "code": "no_output"}},
                status_code=502,
            )
        media = await client.get(urls[0])
        if media.status_code != 200:
            return _upstream_error(media)
        ct = media.headers.get("content-type", "video/mp4")

        async def gen():
            try:
                async for chunk in media.aiter_bytes():
                    yield chunk
            finally:
                await media.aclose()

        return StreamingResponse(
            gen(),
            media_type=ct,
            headers={"Content-Disposition": f'attachment; filename="video_{task_id}.mp4"'},
        )

    @app.get("/v1/videos/{task_id}")
    async def videos_status(task_id: str, request: Request):
        """OpenAI video format: task status / video object."""
        if not _auth_ok(request):
            return _unauthorized()
        client = up.get_client(cfg, "video")
        resp = await client.get(f"{client._raw_base}/v1/videos/tasks/{task_id}")
        if resp.status_code == 404:
            return JSONResponse(
                {"error": {"message": "task_origin_not_exist", "type": "new_api_error",
                           "param": "", "code": "task_not_exist"}},
                status_code=404,
            )
        if resp.status_code != 200:
            return _upstream_error(resp)
        data = resp.json()
        status = _map_video_status(data.get("status", "pending"))
        result = {
            "id": data.get("id", task_id),
            "task_id": data.get("id", task_id),
            "object": "video",
            "status": status,
            "progress": _video_progress(status),
            "created_at": data.get("created_at"),
        }
        if data.get("model"):
            result["model"] = cfg.upstreams["video"].to_client_name(data["model"])
        if status == "completed":
            result["completed_at"] = data.get("completed_at", int(time.time()))
            if _output_urls(data):
                result["video_url"] = f"{request.base_url}v1/videos/{task_id}/content"
        if status == "failed":
            result["error"] = {"message": str(data.get("error") or "generation failed"),
                               "code": "generation_failed"}
        return JSONResponse(result)

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