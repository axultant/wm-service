"""FastAPI app: routes for browsing, inpainting, watermarking and saving.

Run:
    uvicorn app.main:app --host 127.0.0.1 --port 9000
or (supports --mock-inpaint):
    python -m app.main [--mock-inpaint] [--port 9000] [--config config.yaml]
"""
from __future__ import annotations

import io
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError

from . import files
from .config import PROJECT_DIR, Settings, get_settings, save_local_watermark as config_save_local
from .files import PathError
from .inpaint import InpaintError, IOPaintClient, binarize_mask, dilate_mask, png_bytes
from .watermark import WMConfig, apply_watermark, wm_config_from_dict

log = logging.getLogger("wm")

STATIC_DIR = PROJECT_DIR / "static"
MEDIA = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


class BadRequest(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message, self.status = message, status


class BatchManager:
    def __init__(self, settings: Settings, assets: AssetCache):
        self.s = settings
        self.assets = assets
        self.lock = threading.RLock()
        self.job: dict | None = None
        self.executor = ThreadPoolExecutor(max_workers=settings.batch_workers)

    def start(self, paths: list[str], mode: str, skip_processed: bool, overrides: dict) -> dict:
        with self.lock:
            if self.job and self.job["status"] == "running":
                raise BadRequest("A batch job is already running", 409)
            cfg = wm_config_from_dict(overrides, self.s.watermark)
            items = [{"path": p, "status": "pending"} for p in paths]
            job_id = uuid.uuid4().hex
            self.job = {"job_id": job_id, "status": "running", "total": len(items),
                        "completed": 0, "ok": 0, "skipped": 0, "failed": 0,
                        "cancel_requested": False, "items": items}
            self.executor.submit(self._run, self.job, cfg, mode, skip_processed)
            return {"job_id": job_id, "total": len(items)}

    def _run(self, job: dict, cfg: WMConfig, mode: str, skip_processed: bool) -> None:
        logo, font = self.assets.logo(), self.assets.font()
        template_hash = hashlib.sha256(repr(asdict(cfg)).encode()).hexdigest()
        for item in job["items"]:
            with self.lock:
                if job["cancel_requested"]:
                    job["status"] = "cancelled"
                    break
                item["status"] = "running"
            t0 = time.perf_counter()
            try:
                path, root = files.resolve_image(item["path"], self.s.allowed_roots)
                existing = files.processed_status(path, root)
                if skip_processed and existing:
                    item["status"] = "skipped"
                    job["skipped"] += 1
                else:
                    with Image.open(path) as source:
                        img = ImageOps.exif_transpose(source).copy()
                    out = apply_watermark(img, cfg, logo, font)
                    if mode == "copy":
                        res = files.save_copy(out, str(path), self.s.allowed_roots,
                                              jpeg_quality=self.s.jpeg_quality, webp_quality=self.s.webp_quality)
                        result_path = Path(res["path"])
                        result_root = root
                        backup = None
                    else:
                        res = files.save_replace(out, str(path), self.s.allowed_roots,
                                                 backup=self.s.backup, backup_dir_name=self.s.backup_dir_name,
                                                 jpeg_quality=self.s.jpeg_quality, webp_quality=self.s.webp_quality)
                        result_path = path
                        result_root = root
                        backup = res["backup"]
                    digest = hashlib.sha256(result_path.read_bytes()).hexdigest()
                    files.update_state(path, root, template_hash=template_hash,
                                       result_sha256=digest, backup_path=backup)
                    item.update(status="ok", result=res)
                    job["ok"] += 1
                log.info("batch %s %.2fs", path, time.perf_counter() - t0)
            except Exception as e:  # noqa: BLE001 - one bad image must not abort the job
                item.update(status="failed", error=str(e))
                job["failed"] += 1
            finally:
                with self.lock:
                    job["completed"] += 1
        with self.lock:
            if job["status"] == "running":
                job["status"] = "done"

    def snapshot(self, job_id: str) -> dict:
        with self.lock:
            if not self.job or self.job["job_id"] != job_id:
                raise BadRequest("Batch job not found", 404)
            return json.loads(json.dumps(self.job))

    def cancel(self, job_id: str) -> dict:
        with self.lock:
            if not self.job or self.job["job_id"] != job_id:
                raise BadRequest("Batch job not found", 404)
            self.job["cancel_requested"] = True
            return {"ok": True}


def _read_image(upload: UploadFile, what: str = "image") -> Image.Image:
    data = upload.file.read()
    if not data:
        raise BadRequest(f"Empty {what} upload")
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        return im
    except (UnidentifiedImageError, OSError) as e:
        raise BadRequest(f"Could not decode {what}: {e}")


class AssetCache:
    """Loads logo/font lazily and reloads them when the file changes on disk."""

    def __init__(self, settings: Settings):
        self.s = settings
        self._logo: tuple[float, Image.Image | None] | None = None
        self._font: tuple[float, bytes | None] | None = None

    @staticmethod
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return -1.0

    def logo(self) -> Image.Image | None:
        m = self._mtime(self.s.logo_path)
        if self._logo is None or self._logo[0] != m:
            img = None
            if m >= 0:
                with Image.open(self.s.logo_path) as im:
                    img = im.convert("RGBA")
            else:
                log.warning("Logo %s not found: watermark will have no logo", self.s.logo_path)
            self._logo = (m, img)
        return self._logo[1]

    def font(self) -> bytes | None:
        m = self._mtime(self.s.font_path)
        if self._font is None or self._font[0] != m:
            data = self.s.font_path.read_bytes() if m >= 0 else None
            if data is None:
                log.info("Font %s not found: using Pillow's bundled font", self.s.font_path)
            self._font = (m, data)
        return self._font[1]


def create_app(settings: Settings | None = None, client: IOPaintClient | None = None) -> FastAPI:
    s = settings or get_settings()
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    client = client or IOPaintClient(
        s.iopaint_url, timeout=s.inpaint_timeout, hd_strategy=s.hd_strategy,
        crop_margin=s.crop_margin, crop_trigger_size=s.crop_trigger_size, mock=s.mock_inpaint,
    )
    assets = AssetCache(s)
    batches = BatchManager(s, assets)
    app = FastAPI(title="wm-service", docs_url="/docs", redoc_url=None)
    app.state.settings, app.state.client, app.state.assets, app.state.batches = s, client, assets, batches
    if s.mock_inpaint:
        log.warning("MOCK INPAINT enabled: /api/remove returns the input unchanged")

    @app.exception_handler(PathError)
    async def _path_err(_: Request, e: PathError):
        return JSONResponse({"error": e.message}, status_code=e.status)

    @app.exception_handler(BadRequest)
    async def _bad(_: Request, e: BadRequest):
        return JSONResponse({"error": e.message}, status_code=e.status)

    @app.exception_handler(InpaintError)
    async def _inp(_: Request, e: InpaintError):
        return JSONResponse({"error": e.message}, status_code=e.status)

    @app.exception_handler(ValueError)
    async def _val(_: Request, e: ValueError):
        return JSONResponse({"error": str(e)}, status_code=400)

    # ------------------------------------------------------------------ meta
    @app.get("/api/config")
    def api_config():
        return {
            "roots": [str(r) for r in s.allowed_roots],
            "iopaint": client.health(),
            "default_dilate_px": s.default_dilate_px,
            "jpeg_quality": s.jpeg_quality,
            "webp_quality": s.webp_quality,
            "backup": s.backup,
            "watermark": asdict(s.watermark),
            "has_logo": assets.logo() is not None,
            "has_font": assets.font() is not None,
        }

    @app.post("/api/config/default")
    async def api_config_default(request: Request):
        body = await request.json()
        cfg = wm_config_from_dict(body.get("watermark") if isinstance(body, dict) else body, s.watermark)
        path = config_save_local(cfg)
        s.watermark = cfg
        return {"path": str(path), "watermark": asdict(cfg)}

    @app.get("/api/health")
    def api_health():
        return client.health()

    # ------------------------------------------------------------------ browsing
    @app.get("/api/folders")
    def api_folders(path: str = ""):
        return files.list_folders(path, s.allowed_roots)

    @app.get("/api/images")
    def api_images(path: str):
        return files.list_images(path, s.allowed_roots)

    @app.get("/api/thumb")
    def api_thumb(path: str, w: int = Query(256, ge=16, le=2048)):
        p, _ = files.resolve_image(path, s.allowed_roots)
        out = files.thumbnail(p, w, s.cache_dir)
        return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/image")
    def api_image(path: str):
        p, _ = files.resolve_image(path, s.allowed_roots)
        try:
            data, (w, h) = files.load_transposed_png(p)
        except (UnidentifiedImageError, OSError) as e:
            raise BadRequest(f"Could not decode image: {e}")
        return Response(data, media_type="image/png", headers={
            "Cache-Control": "no-store", "X-Image-Width": str(w), "X-Image-Height": str(h)})

    @app.get("/api/state")
    def api_state(path: str):
        return files.state_for_folder(path, s.allowed_roots)

    @app.post("/api/restore")
    async def api_restore(request: Request):
        if "application/json" in request.headers.get("content-type", ""):
            body = await request.json()
            path = body.get("path") if isinstance(body, dict) else None
        else:
            form = await request.form()
            path = form.get("path")
        if not isinstance(path, str) or not path:
            raise BadRequest("path is required")
        return files.restore_original(path, s.allowed_roots, s.backup_dir_name)

    @app.post("/api/remove")
    def api_remove(
        image: UploadFile = File(...),
        mask: UploadFile = File(...),
        dilate_px: int = Form(6),
    ):
        t0 = time.perf_counter()
        if not 0 <= dilate_px <= 100:
            raise BadRequest("dilate_px must be between 0 and 100")
        img = _read_image(image, "image")
        msk = _read_image(mask, "mask")
        if img.size != msk.size:
            raise BadRequest(f"Mask size {msk.size} differs from image size {img.size}")
        has_alpha = "A" in img.getbands()
        alpha = img.getchannel("A") if has_alpha else None
        rgb = img.convert("RGB")
        m = dilate_mask(binarize_mask(msk), dilate_px)
        if m.getbbox() is None:
            raise BadRequest("Mask is empty: paint over the watermark first")
        out = client.inpaint(rgb, m)
        if alpha is not None:
            out.putalpha(alpha)
        data = png_bytes(out)
        log.info("remove %dx%d dilate=%d mask_bbox=%s -> %.2fs%s", img.width, img.height,
                 dilate_px, m.getbbox(), time.perf_counter() - t0, " [mock]" if client.mock else "")
        return Response(data, media_type="image/png")

    def _wm_cfg(**overrides) -> WMConfig:
        over = {k: v for k, v in overrides.items() if v is not None}
        return replace(s.watermark, **over).validate()

    @app.post("/api/watermark")
    def api_watermark(
        image: UploadFile = File(...),
        fmt: str = Form("png"),
        quality: int = Form(95),
        logo_width: float | None = Form(None),
        text_opacity: float | None = Form(None),
        text_angle: float | None = Form(None),
        logo_margin_x: float | None = Form(None),
        logo_margin_y: float | None = Form(None),
        logo_anchor_x: float | None = Form(None),
        logo_anchor_y: float | None = Form(None),
        logo_enabled: bool | None = Form(None),
        text_enabled: bool | None = Form(None),
    ):
        t0 = time.perf_counter()
        fmt = fmt.lower()
        if fmt not in ("png", "jpeg", "jpg", "webp"):
            raise BadRequest("fmt must be png, jpeg or webp")
        if not 1 <= quality <= 100:
            raise BadRequest("quality must be 1..100")
        img = _read_image(image)
        cfg = _wm_cfg(logo_width=logo_width, text_opacity=text_opacity, text_angle=text_angle,
                      logo_margin_x=logo_margin_x, logo_margin_y=logo_margin_y,
                      logo_anchor_x=logo_anchor_x, logo_anchor_y=logo_anchor_y,
                      logo_enabled=logo_enabled, text_enabled=text_enabled)
        out = apply_watermark(img, cfg, assets.logo(), assets.font())
        if "A" not in img.getbands():
            out = out.convert("RGB")
        buf = io.BytesIO()
        if fmt == "png":
            out.save(buf, "PNG", compress_level=1)
            media = "image/png"
        elif fmt == "webp":
            out.save(buf, "WEBP", quality=quality)
            media = "image/webp"
        else:
            out.convert("RGB").save(buf, "JPEG", quality=quality, subsampling=0)
            media = "image/jpeg"
        log.info("watermark %dx%d fmt=%s cfg(logo=%.3f op=%.2f ang=%.1f) -> %.2fs", out.width, out.height,
                 fmt, cfg.logo_width, cfg.text_opacity, cfg.text_angle, time.perf_counter() - t0)
        return Response(buf.getvalue(), media_type=media)

    @app.post("/api/export")
    def api_export(image: UploadFile = File(...), path: str = Form(...)):
        """Encode the final (already watermarked) image in the original file's format/quality.

        Same encoder as /api/save, so Download and Save produce identical bytes.
        """
        t0 = time.perf_counter()
        p, _ = files.resolve_image(path, s.allowed_roots)
        img = _read_image(image)
        enc = files.encode_like_original(img, p, s.jpeg_quality, s.webp_quality)
        log.info("export %dx%d %s -> %d bytes %.2fs", img.width, img.height, enc.fmt, len(enc.data),
                 time.perf_counter() - t0)
        return Response(enc.data, media_type=MEDIA[enc.fmt], headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(p.name)}"})

    @app.post("/api/save")
    def api_save(image: UploadFile = File(...), path: str = Form(...)):
        t0 = time.perf_counter()
        img = _read_image(image)
        res = files.save_replace(
            img, path, s.allowed_roots, backup=s.backup, backup_dir_name=s.backup_dir_name,
            jpeg_quality=s.jpeg_quality, webp_quality=s.webp_quality)
        saved_path, root = files.resolve_image(path, s.allowed_roots)
        files.update_state(saved_path, root, template_hash="single", result_sha256=hashlib.sha256(saved_path.read_bytes()).hexdigest(),
                           backup_path=res["backup"])
        log.info("save %dx%d -> %s (%s, %d bytes, backup=%s%s) %.2fs", img.width, img.height, res["path"],
                 res["format"], res["size"], res["backup"], " new" if res["backup_created"] else "",
                 time.perf_counter() - t0)
        return res

    @app.post("/api/batch/start")
    async def api_batch_start(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise BadRequest("JSON object required")
        paths = body.get("paths")
        mode = body.get("mode", "replace")
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
            raise BadRequest("paths must be a non-empty list of strings")
        if mode not in ("replace", "copy"):
            raise BadRequest("mode must be replace or copy")
        for raw in paths:
            files.resolve_image(raw, s.allowed_roots)
        overrides = body.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise BadRequest("overrides must be an object")
        return batches.start(paths, mode, bool(body.get("skip_processed", True)), overrides)

    @app.get("/api/batch/{job_id}")
    def api_batch_status(job_id: str):
        return batches.snapshot(job_id)

    @app.post("/api/batch/{job_id}/cancel")
    def api_batch_cancel(job_id: str):
        return batches.cancel(job_id)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app() if __name__ != "__main__" else None


def main() -> None:
    import argparse

    import uvicorn

    from . import config as config_mod

    ap = argparse.ArgumentParser(description="wm-service")
    ap.add_argument("--mock-inpaint", action="store_true", help="return the input unchanged instead of calling IOPaint")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--config", default=None, help="path to config.yaml")
    a = ap.parse_args()
    if a.mock_inpaint:
        os.environ["WM_MOCK_INPAINT"] = "1"
    if a.config:
        os.environ["WM_CONFIG"] = a.config
    config_mod.get_settings.cache_clear()
    s = config_mod.get_settings()
    host = a.host or s.host
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("Binding to %s: this tool is meant to be local-only", host)
    uvicorn.run(create_app(s), host=host, port=a.port or s.port)


if __name__ == "__main__":
    main()
