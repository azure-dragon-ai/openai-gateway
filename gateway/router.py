"""OpenAI-compatible gateway routes.

The gateway forwards the client's original /v1 path verbatim to the
configured upstream root (base_url is the service root WITHOUT /v1),
so every subpath is a transparent passthrough:

  GET  /health
  GET  /v1/models
  POST /v1/chat/completions           (streaming SSE + non-streaming)
  POST /v1/videos                      (OpenAI video format: create async task)
  GET  /v1/videos/{task_id}            (OpenAI video format: task status)
  GET  /v1/videos/{task_id}/content    (OpenAI video format: proxy download)
  POST /v1/audio/speech                (TTS, proxied to the audio upstream)
  POST /v1/{category}/{subpath:path}   (transparent proxy, e.g. /generations)
  GET  /v1/{category}/{subpath:path}   (transparent proxy, e.g. /tasks/{id})

Category -> URL prefix mapping is data-driven: any section in config.yaml
that carries a base_url gets its own proxy mount point.
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

#: Category -> URL prefix mounted under /v1. `chat` is special-cased above.
CATEGORY_PREFIX: dict[str, str] = {
    "image": "/v1/images",
    "video": "/v1/videos",
    "audio": "/v1/audio",
}


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


# OpenAI's TTS `response_format` is a CODEC (mp3/opus/aac/flac/wav/pcm).
# Upstream reuses the same field name for the DELIVERY MODE
# (b64_json/url/file/path) - the codec is fixed by the workflow's
# SaveAudio node. Sending a codec therefore always 400s, so translate it.
_AUDIO_CODECS = {"mp3", "opus", "aac", "flac", "wav", "pcm"}
_UPSTREAM_AUDIO_DELIVERY = {"b64_json", "url", "file", "path"}

# Upstream feeds `input` to the voice designer as the reference text and
# rejects anything shorter ("Reference text must contain at least 10
# characters"). Fail fast here with a readable message instead of letting
# ComfyUI blow up three layers down.
_MIN_AUDIO_INPUT_CHARS = 10

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
    m = re.search(r'boundary=(\"([^\"]+)\"|([^;]+))', content_type)
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

    app = FastAPI(title="openai-gateway", version="1.1.0", lifespan=lifespan)
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

    def _guard(request: Request):
        return None if _auth_ok(request) else _unauthorized()

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "upstreams": {
                name: {
                    "base_url": c.base_url,
                    "models": list(c.models),
                    "serialize": c.serialize,
                }
                for name, c in cfg.upstreams.items()
            },
        }

    @app.get("/v1/models")
    async def list_models(request: Request):
        denied = _guard(request)
        if denied:
            return denied
        data = [
            {"id": model, "object": "model", "created": 0, "owned_by": category}
            for category, c in cfg.upstreams.items()
            for model in c.models
        ]
        return {"object": "list", "data": data}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        denied = _guard(request)
        if denied:
            return denied
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

    async def _dispatch(request: Request, category: str):
        """Perform the upstream call for a proxy request."""
        up_cfg = cfg.upstreams[category]
        client = up.get_client(cfg, category)
        target = _upstream_url(client._raw_base, request)

        raw = await request.body()
        body = None
        content_type = request.headers.get("content-type", "")
        if request.method in ("POST", "PUT") and raw:
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
            # Forward the original Content-Type verbatim. Multipart bodies
            # carry their `boundary=` inside this header, and upstream
            # multipart readers (aiohttp request.multipart()) raise
            # KeyError('Content-Type') without it.
            fwd = {"content-type": content_type} if content_type else {}
            resp = await client.request(request.method, target, content=raw, headers=fwd)

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

    async def _proxy(request: Request, category: str, subpath: str = ""):
        """Transparent proxy: forward the client's /v1 path verbatim."""
        denied = _guard(request)
        if denied:
            return denied
        up_cfg = cfg.upstreams[category]

        # Generation endpoints for a serialized category must run one at a
        # time; status polls (GET) are cheap and must not queue behind them.
        needs_lock = up_cfg.serialize and request.method in ("POST", "PUT")
        if not needs_lock:
            return await _dispatch(request, category)

        async with up.get_lock(category):
            return await _dispatch(request, category)

    @app.post("/v1/videos")
    async def videos_create(request: Request):
        """OpenAI video format: create an async video task."""
        denied = _guard(request)
        if denied:
            return denied
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
        seconds = body.get("seconds")
        size = body.get("size")
        body.pop("seconds", None)

        client = up.get_client(cfg, "video")
        url = f"{client._raw_base}/v1/videos/generations"

        async def _create():
            resp = await client.post(url, json=body)
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
            if seconds is not None:
                result["seconds"] = seconds
            if size is not None:
                result["size"] = size
            return JSONResponse(result)

        if not up_cfg.serialize:
            return await _create()
        async with up.get_lock("video"):
            return await _create()

    @app.get("/v1/videos/{task_id}/content")
    async def videos_content(task_id: str, request: Request):
        """OpenAI video format: proxy download of the finished video."""
        denied = _guard(request)
        if denied:
            return denied
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
        denied = _guard(request)
        if denied:
            return denied
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

    @app.post("/v1/audio/speech")
    async def audio_speech(request: Request):
        """TTS: adapt the OpenAI-style payload, then proxy upstream.

        Registered before the generic /v1/audio/{subpath} mount so it wins.
        """
        denied = _guard(request)
        if denied:
            return denied
        if "audio" not in cfg.upstreams:
            return _bad_request("no 'audio' upstream configured in config.yaml")

        try:
            body = json.loads(await request.body())
        except (ValueError, TypeError):
            return _bad_request("invalid JSON body")
        if not isinstance(body, dict):
            return _bad_request("JSON body must be an object")

        up_cfg = cfg.upstreams["audio"]
        client = up.get_client(cfg, "audio")

        if body.get("model"):
            body["model"] = up_cfg.resolve_model(body["model"])

        # OpenAI clients send a codec; upstream expects a delivery mode.
        # A codec is never valid upstream, so drop it and let upstream use
        # its default delivery. A real delivery value passes through.
        fmt = body.get("response_format")
        if isinstance(fmt, str):
            lowered = fmt.strip().lower()
            if lowered in _AUDIO_CODECS:
                log.debug("audio: dropping OpenAI codec response_format=%s", fmt)
                body.pop("response_format")
            elif lowered not in _UPSTREAM_AUDIO_DELIVERY:
                return _bad_request(
                    "invalid 'response_format': upstream expects one of "
                    "b64_json, url, file, path (it selects delivery mode, "
                    "not the codec)"
                )

        text = body.get("input")
        if not text:
            return _bad_request("'input' is required")
        if isinstance(text, str) and len(text.strip()) < _MIN_AUDIO_INPUT_CHARS:
            return _bad_request(
                "'input' must be at least %d characters: upstream uses it as "
                "the voice-design reference text" % _MIN_AUDIO_INPUT_CHARS
            )

        url = f"{client._raw_base}/v1/audio/speech"

        async def _create():
            resp = await client.post(url, json=body)
            if resp.status_code != 200:
                return _upstream_error(resp)
            ct = resp.headers.get("content-type", "")
            if ct.startswith("application/json"):
                data = resp.json()
                if isinstance(data, dict) and isinstance(data.get("model"), str):
                    data["model"] = up_cfg.to_client_name(data["model"])
                return JSONResponse(data)
            return Response(content=resp.content, status_code=200, media_type=ct)

        if not up_cfg.serialize:
            return await _create()
        async with up.get_lock("audio"):
            return await _create()

    # -- data-driven proxy mounts ------------------------------------------
    # Registered for every configured category, so adding a section to
    # config.yaml is enough to expose its endpoint (no code change needed).
    for _category, _prefix in CATEGORY_PREFIX.items():
        if _category not in cfg.upstreams:
            continue

        def _make(cat: str):
            async def _handler(request: Request, subpath: str):
                return await _proxy(request, cat, subpath)

            return _handler

        app.post(f"{_prefix}/{{subpath:path}}")(_make(_category))
        app.get(f"{_prefix}/{{subpath:path}}")(_make(_category))

    # Categories without a prefix in CATEGORY_PREFIX fall back to /v1/<name>.
    for _category in cfg.upstreams:
        if _category in CATEGORY_PREFIX or _category == "chat":
            continue

        def _make_extra(cat: str):
            async def _handler(request: Request, subpath: str):
                return await _proxy(request, cat, subpath)

            return _handler

        app.post(f"/v1/{_category}/{{subpath:path}}")(_make_extra(_category))
        app.get(f"/v1/{_category}/{{subpath:path}}")(_make_extra(_category))

    log.info(
        "gateway routes ready (categories: %s)",
        ", ".join(f"{c}={len(u.models)}" for c, u in cfg.upstreams.items()),
    )
    return app
