"""Backlot server — FastAPI app: board state API, SSE change feed, media.

The watcher observes ``projects/`` with watchfiles; on any change it bumps a
per-project version and wakes SSE subscribers, who tell the browser to
refetch state. The board/library routes never write to project directories. The only write
routes are the affiliate toolkit (``/api/affiliate/*``), which are limited to
local origins and create projects via ``lib.checkpoint.init_project``.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from urllib.parse import urlparse

from backlot import affiliate
from backlot.state import (
    PROJECTS_DIR,
    REPO_ROOT,
    list_projects,
    load_board_state,
    series_from_disk,
    summarize_project,
)

UI_DIR = Path(__file__).resolve().parent / "ui"
THUMB_CACHE_DIR = REPO_ROOT / ".backlot" / "thumbs"
THUMB_WIDTHS = (320, 640, 960)

# Paths inside a project whose changes are pure noise for the board.
_IGNORE_PARTS = {"node_modules", ".git", "__pycache__", ".cache"}

SSE_HEARTBEAT_SECONDS = 15


_LOCAL_HOSTS = {"127.0.0.1", "localhost"}


class HooksRequest(BaseModel):
    template: str
    language: str = "vi"
    count: int = Field(default=6, ge=1, le=24)


class CopyRequest(BaseModel):
    platform: str = "tiktok"
    language: str = "vi"
    hook: str = Field(min_length=1, max_length=500)
    cta: str = Field(default="", max_length=500)


class ComplianceRequest(BaseModel):
    text: str = Field(max_length=20000)
    platform: str = "tiktok"
    language: str = "vi"
    network: str = "other"
    has_affiliate_link: bool = True


class BatchRequest(BaseModel):
    product_id: str
    templates: list[str] = Field(min_length=1, max_length=10)
    language: str = "vi"
    platform: str = "tiktok"
    hooks_per_template: int = Field(default=1, ge=1, le=5)
    aspect_ratios: Optional[list[str]] = None
    series: Optional[str] = Field(default=None, max_length=80)


def _ui_html(name: str, assets: tuple[str, ...]) -> HTMLResponse:
    html = (UI_DIR / name).read_text(encoding="utf-8")
    for asset in assets:
        path = UI_DIR / asset
        if path.is_file():
            version = str(int(path.stat().st_mtime))
            html = html.replace(f"/ui/{asset}", f"/ui/{asset}?v={version}")
    return HTMLResponse(html)


class ChangeHub:
    """Fan-out of project-change notifications to SSE subscribers.

    Subscriptions are filtered: a board subscribed to one project only ever
    receives that project's ids, so unrelated-project bursts can't flood its
    queue and starve out the one notification it actually needs.
    """

    def __init__(self) -> None:
        self._subscribers: dict[asyncio.Queue, Optional[str]] = {}

    def subscribe(self, project_id: Optional[str] = None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers[q] = project_id
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.pop(q, None)

    def publish(self, project_id: str) -> None:
        for q, only in list(self._subscribers.items()):
            if only is not None and only != project_id:
                continue
            try:
                q.put_nowait(project_id)
            except asyncio.QueueFull:
                # Queue holds only THIS subscriber's relevant ids, so a full
                # queue already guarantees a pending wake-up → safe to drop.
                pass


hub = ChangeHub()

# Library summaries are expensive to derive (full state parse per project);
# cache per project and invalidate from the watcher.
_summary_cache: dict[str, dict] = {}


def _invalidate_summary(project_id: str) -> None:
    _summary_cache.pop(project_id, None)


def _cached_summaries() -> list[dict]:
    if not PROJECTS_DIR.is_dir():
        return []
    summaries = []
    for entry in sorted(PROJECTS_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith(("_", ".")):
            continue
        cached = _summary_cache.get(entry.name)
        if cached is None:
            try:
                cached = summarize_project(entry)
            except Exception:
                cached = {
                    "project_id": entry.name, "title": entry.name,
                    "series": series_from_disk(entry),
                    "pipeline_type": "unknown", "has_pipeline_state": False,
                    "poster": None, "live": False, "last_activity": 0,
                    "active_stage": None, "awaiting_human": False,
                    "stage_states": [], "completed_count": 0,
                    "render_count": 0, "scene_count": 0, "error": "unreadable",
                }
            _summary_cache[entry.name] = cached
        summaries.append(cached)
    summaries.sort(key=lambda s: (not s["live"], -(s["last_activity"] or 0)))
    return summaries


# Watch-loop hot path: pure string comparison, no per-path filesystem calls
# (change batches can be thousands of paths during a render).
import os as _os

_PROJECTS_ROOT_STR = _os.path.normcase(str(PROJECTS_DIR.resolve()))


def _project_of_change(path_str: str) -> Optional[str]:
    """Map a changed filesystem path to a project id (None = irrelevant)."""
    norm = _os.path.normcase(_os.path.normpath(path_str))
    if not norm.startswith(_PROJECTS_ROOT_STR):
        return None
    rel = norm[len(_PROJECTS_ROOT_STR):].lstrip("\\/")
    if not rel:
        return None
    parts = rel.replace("\\", "/").split("/")
    if _IGNORE_PARTS.intersection(parts):
        return None
    return parts[0]


async def _watch_projects() -> None:
    """Background task: watch projects/ and publish debounced changes."""
    try:
        from watchfiles import awatch
    except ImportError:
        return  # watcher unavailable → board still works via manual refresh
    if not PROJECTS_DIR.is_dir():
        return
    async for changes in awatch(PROJECTS_DIR, recursive=True, step=400):
        touched: set[str] = set()
        for _change, path_str in changes:
            pid = _project_of_change(path_str)
            if pid:
                touched.add(pid)
        for pid in touched:
            _invalidate_summary(pid)
            hub.publish(pid)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Own and cleanly stop the project watcher with FastAPI's lifespan API."""

    task = asyncio.create_task(_watch_projects())
    app.state.watch_task = task
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    app = FastAPI(title="Backlot", docs_url=None, redoc_url=None, lifespan=_lifespan)

    # ---- API ----------------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "app": "backlot"}

    @app.get("/api/projects")
    async def projects() -> list:
        return await asyncio.to_thread(_cached_summaries)

    @app.get("/api/project/{project_id}/state")
    async def project_state(project_id: str) -> dict:
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(load_board_state, project_dir)

    @app.get("/api/project/{project_id}/events")
    async def project_events(project_id: str, request: Request) -> StreamingResponse:
        _safe_project_dir(project_id)  # 404 early for unknown projects

        async def stream():
            q = hub.subscribe(project_id)
            try:
                yield _sse({"type": "hello", "project_id": project_id})
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        await asyncio.wait_for(q.get(), timeout=SSE_HEARTBEAT_SECONDS)
                    except asyncio.TimeoutError:
                        yield _sse({"type": "heartbeat", "ts": time.time()})
                        continue
                    # Coalesce bursts: drain anything else queued.
                    while not q.empty():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    yield _sse({"type": "change", "project_id": project_id})
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    @app.get("/api/library/events")
    async def library_events(request: Request) -> StreamingResponse:
        async def stream():
            q = hub.subscribe()
            try:
                yield _sse({"type": "hello"})
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        changed = await asyncio.wait_for(q.get(), timeout=SSE_HEARTBEAT_SECONDS)
                    except asyncio.TimeoutError:
                        yield _sse({"type": "heartbeat", "ts": time.time()})
                        continue
                    while not q.empty():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    yield _sse({"type": "change", "project_id": changed})
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    # ---- Thumbnails (downscaled, cached on disk) ------------------------

    @app.get("/thumb/{project_id}/{file_path:path}")
    async def thumb(project_id: str, file_path: str, w: int = 640) -> FileResponse:
        project_dir = _safe_project_dir(project_id)
        target = (project_dir / file_path).resolve()
        try:
            target.relative_to(project_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="path escapes project")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="media not found")
        width = min(THUMB_WIDTHS, key=lambda x: abs(x - w))
        cached = await asyncio.to_thread(_thumbnail_for, target, width)
        if cached is None:
            # Never fall back to raw video bytes for an <img> consumer (F-03);
            # non-thumbable images are safe to serve as-is.
            if target.suffix.lower() in {".mp4", ".webm", ".mov"}:
                raise HTTPException(status_code=404, detail="no poster frame available")
            return FileResponse(target)
        return FileResponse(cached, media_type="image/jpeg")

    # ---- Media (range requests handled by FileResponse) ---------------

    @app.get("/media/{project_id}/{file_path:path}")
    async def media(project_id: str, file_path: str) -> FileResponse:
        project_dir = _safe_project_dir(project_id)
        target = (project_dir / file_path).resolve()
        try:
            target.relative_to(project_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="path escapes project")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="media not found")
        return FileResponse(target)

    # ---- Affiliate toolkit -------------------------------------------

    @app.exception_handler(affiliate.AffiliateError)
    async def affiliate_error(request: Request, exc: affiliate.AffiliateError) -> JSONResponse:
        status = 404 if isinstance(exc, affiliate.NotFoundError) else 400
        return JSONResponse({"detail": str(exc)}, status_code=status)

    @app.middleware("http")
    async def local_origin_only_for_writes(request, call_next):
        # Host checking stops DNS rebinding; this stops other websites from
        # making the browser POST to localhost (CSRF).
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).hostname not in _LOCAL_HOSTS:
                return JSONResponse({"detail": "cross-origin write blocked"}, status_code=403)
        return await call_next(request)

    @app.get("/api/affiliate/meta")
    async def aff_meta() -> dict:
        return {
            "templates": affiliate.list_templates(),
            "platforms": sorted(affiliate.PLATFORMS),
            "languages": list(affiliate.LANGUAGES),
            "networks": list(affiliate.NETWORKS),
            "max_batch": affiliate.MAX_BATCH,
        }

    @app.get("/api/affiliate/products")
    async def aff_products() -> list:
        return await asyncio.to_thread(affiliate.list_products)

    @app.post("/api/affiliate/products")
    async def aff_product_create(payload: affiliate.ProductIn) -> dict:
        return await asyncio.to_thread(affiliate.save_product, payload)

    @app.put("/api/affiliate/products/{product_id}")
    async def aff_product_update(product_id: str, payload: affiliate.ProductIn) -> dict:
        return await asyncio.to_thread(affiliate.save_product, payload, product_id)

    @app.delete("/api/affiliate/products/{product_id}")
    async def aff_product_delete(product_id: str) -> dict:
        await asyncio.to_thread(affiliate.delete_product, product_id)
        return {"ok": True}

    @app.put("/api/affiliate/products/{product_id}/image")
    async def aff_image_upload(product_id: str, request: Request) -> dict:
        # Raw image bytes in the body (no multipart dependency). Reject early
        # on the declared size, then re-check after reading.
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > affiliate.MAX_IMAGE_BYTES:
            raise affiliate.AffiliateError(
                f"Ảnh quá lớn (tối đa {affiliate.MAX_IMAGE_BYTES // (1024 * 1024)} MB)")
        data = await request.body()
        return await asyncio.to_thread(affiliate.save_product_image, product_id, data)

    @app.delete("/api/affiliate/products/{product_id}/image")
    async def aff_image_delete(product_id: str) -> dict:
        return await asyncio.to_thread(affiliate.delete_product_image, product_id)

    @app.get("/api/affiliate/products/{product_id}/image")
    async def aff_image_get(product_id: str) -> FileResponse:
        path = await asyncio.to_thread(affiliate.product_image_path, product_id)
        return FileResponse(path, headers={"X-Content-Type-Options": "nosniff"})

    @app.post("/api/affiliate/products/{product_id}/hooks")
    async def aff_hooks(product_id: str, payload: HooksRequest) -> dict:
        product = affiliate.get_product(product_id)
        return affiliate.generate_hooks(product, payload.template, payload.language, payload.count)

    @app.post("/api/affiliate/products/{product_id}/copy")
    async def aff_copy(product_id: str, payload: CopyRequest) -> dict:
        product = affiliate.get_product(product_id)
        copy = affiliate.build_copy(product, payload.platform, payload.language, payload.hook, payload.cta)
        copy["compliance"] = affiliate.check_compliance(
            copy["caption"], payload.platform, bool(product.get("affiliate_url")),
            product.get("network", "other"), payload.language)
        return copy

    @app.post("/api/affiliate/compliance")
    async def aff_compliance(payload: ComplianceRequest) -> dict:
        return affiliate.check_compliance(
            payload.text, payload.platform, payload.has_affiliate_link,
            payload.network, payload.language)

    @app.post("/api/affiliate/batch")
    async def aff_batch(payload: BatchRequest) -> dict:
        return await asyncio.to_thread(
            lambda: affiliate.create_batch(
                payload.product_id, payload.templates, language=payload.language,
                platform=payload.platform, hooks_per_template=payload.hooks_per_template,
                aspect_ratios=payload.aspect_ratios, series=payload.series))

    @app.get("/api/affiliate/results")
    async def aff_results() -> dict:
        return await asyncio.to_thread(affiliate.results_summary)

    @app.post("/api/affiliate/results")
    async def aff_result_add(payload: affiliate.ResultIn) -> dict:
        return await asyncio.to_thread(affiliate.add_result, payload)

    @app.delete("/api/affiliate/results/{result_id}")
    async def aff_result_delete(result_id: str) -> dict:
        await asyncio.to_thread(affiliate.delete_result, result_id)
        return {"ok": True}

    @app.get("/affiliate")
    async def affiliate_page() -> HTMLResponse:
        return _ui_html("affiliate.html", ("board.css", "affiliate.css", "affiliate.js"))

    # ---- UI ------------------------------------------------------------

    @app.get("/p/{project_id}")
    async def board_page(project_id: str) -> HTMLResponse:
        return _ui_html("board.html", ("board.css", "board.js"))

    @app.get("/p/{project_path:path}")
    async def board_page_path(project_path: str) -> HTMLResponse:
        return _ui_html("board.html", ("board.css", "board.js"))

    @app.get("/")
    async def library_page() -> HTMLResponse:
        return _ui_html("index.html", ("board.css", "library.js"))

    if UI_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")

    # The board is a long-lived SPA: a tab keeps running whatever board.js it
    # loaded, and browsers heuristically cache /ui assets. no-cache forces a
    # conditional revalidation (cheap 304 via ETag) on every load so UI fixes
    # show up on a plain refresh. Media/thumb responses keep normal caching.
    @app.middleware("http")
    async def ui_no_cache(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path in ("/", "/affiliate") or path.startswith("/ui") or path.startswith("/p/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # Loopback binding alone does not prevent browser DNS-rebinding requests.
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"], www_redirect=False,
    )
    return app


def _safe_project_dir(project_id: str) -> Path:
    # ':' rejects Windows drive-relative ids like "C:" (PROJECTS_DIR / "C:"
    # collapses back to PROJECTS_DIR itself).
    if any(c in project_id for c in "/\\:") or project_id in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid project id")
    project_dir = PROJECTS_DIR / project_id
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"unknown project: {project_id}")
    return project_dir


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _thumbnail_for(source: Path, width: int) -> Optional[Path]:
    """Downscale an image (or extract a video poster frame) to a cached JPEG."""
    suffix = source.suffix.lower()
    is_image = suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    is_video = suffix in {".mp4", ".webm", ".mov"}
    if not (is_image or is_video):
        return None
    try:
        import hashlib
        stat = source.stat()
        key = hashlib.sha1(
            f"{source}|{stat.st_mtime_ns}|{stat.st_size}|{width}".encode()
        ).hexdigest()[:20]
        cached = THUMB_CACHE_DIR / f"{key}.jpg"
        if cached.is_file():
            return cached
        THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # Unique temp per request — concurrent misses for the same source
        # must not write (and replace from) the same temp file.
        import uuid
        tmp = THUMB_CACHE_DIR / f"{key}.{uuid.uuid4().hex[:8]}.tmp.jpg"
        if is_video:
            import subprocess
            result = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", "1.5",
                 "-i", str(source), "-frames:v", "1",
                 "-vf", f"scale={width}:-2", str(tmp)],
                capture_output=True, timeout=30,
            )
            if result.returncode != 0 or not tmp.is_file():
                return None
        else:
            from PIL import Image
            with Image.open(source) as img:
                img = img.convert("RGB")
                img.thumbnail((width, width * 3))
                img.save(tmp, "JPEG", quality=82)
        tmp.replace(cached)
        return cached
    except Exception:
        return None


app = create_app()
