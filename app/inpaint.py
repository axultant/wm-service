"""IOPaint HTTP client + mask preparation.

Written against the schema served by the installed IOPaint at /openapi.json:

    POST /api/v1/inpaint   (application/json, InpaintRequest)
        image: str   base64 encoded image
        mask:  str   base64 encoded mask (white = inpaint)
        hd_strategy: str                      "Crop" | "Resize" | "Original"
        hd_strategy_crop_margin: int          default 128
        hd_strategy_crop_trigger_size: int    default 800
        ... (other fields left at server defaults)
    -> 200: raw image bytes (same format as the input image), 422 on validation error.

    GET /api/v1/server-config  -> used as health check.
"""
from __future__ import annotations

import base64
import io
import logging

import cv2
import httpx
import numpy as np
from PIL import Image

log = logging.getLogger("wm.inpaint")

INPAINT_PATH = "/api/v1/inpaint"
HEALTH_PATH = "/api/v1/server-config"


class InpaintError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.message = message
        self.status = status


def png_bytes(img: Image.Image, compress_level: int = 1) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=compress_level)
    return buf.getvalue()


def binarize_mask(mask: Image.Image, threshold: int = 32) -> Image.Image:
    """Any mask image -> L mode, 0/255. Transparent pixels count as black."""
    if mask.mode in ("RGBA", "LA", "PA") or (mask.mode == "P" and "transparency" in mask.info):
        rgba = mask.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
        mask = Image.alpha_composite(bg, rgba)
    gray = mask.convert("L")
    return gray.point(lambda v: 255 if v > threshold else 0)


def dilate_mask(mask: Image.Image, px: int) -> Image.Image:
    """Dilate a binary L mask by `px` pixels with an elliptical kernel."""
    arr = np.asarray(binarize_mask(mask), dtype=np.uint8)
    if px > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * px + 1, 2 * px + 1))
        arr = cv2.dilate(arr, kernel)
    return Image.fromarray(arr, mode="L")


class IOPaintClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 300.0,
        hd_strategy: str = "Crop",
        crop_margin: int = 128,
        crop_trigger_size: int = 800,
        mock: bool = False,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.hd_strategy = hd_strategy
        self.crop_margin = crop_margin
        self.crop_trigger_size = crop_trigger_size
        self.mock = mock
        self._transport = transport

    def _client(self, timeout: float) -> httpx.Client:
        # trust_env=False: never route local calls through a system proxy.
        return httpx.Client(timeout=timeout, transport=self._transport, trust_env=False)

    def build_payload(self, image_png: bytes, mask_png: bytes) -> dict:
        return {
            "image": base64.b64encode(image_png).decode("ascii"),
            "mask": base64.b64encode(mask_png).decode("ascii"),
            "hd_strategy": self.hd_strategy,
            "hd_strategy_crop_margin": self.crop_margin,
            "hd_strategy_crop_trigger_size": self.crop_trigger_size,
        }

    def health(self) -> dict:
        if self.mock:
            return {"ok": True, "mock": True, "url": self.base_url}
        try:
            with self._client(3.0) as c:
                r = c.get(self.base_url + HEALTH_PATH)
            r.raise_for_status()
            return {"ok": True, "mock": False, "url": self.base_url}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "mock": False, "url": self.base_url, "error": self._describe(e)}

    def _describe(self, e: Exception) -> str:
        if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
            return (f"IOPaint is not reachable at {self.base_url}. Start it first: "
                    f"iopaint start --model lama --port 8080 (or fix iopaint_url / IOPAINT_URL).")
        if isinstance(e, httpx.TimeoutException):
            return f"IOPaint did not answer within {self.timeout:.0f} s."
        return f"IOPaint request failed: {e}"

    def inpaint(self, image: Image.Image, mask: Image.Image) -> Image.Image:
        """image: RGB, mask: L (255 = remove), same size. Returns RGB image of same size."""
        if image.size != mask.size:
            raise InpaintError("Image and mask sizes differ", 400)
        if self.mock:
            return image.copy()
        payload = self.build_payload(png_bytes(image), png_bytes(mask))
        try:
            with self._client(self.timeout) as c:
                r = c.post(self.base_url + INPAINT_PATH, json=payload)
        except httpx.TimeoutException as e:
            raise InpaintError(self._describe(e), 504) from e
        except httpx.HTTPError as e:
            raise InpaintError(self._describe(e), 502) from e
        if r.status_code != 200:
            detail = r.text[:500]
            raise InpaintError(f"IOPaint returned HTTP {r.status_code}: {detail}", 502)
        try:
            out = Image.open(io.BytesIO(r.content))
            out.load()
        except Exception as e:  # noqa: BLE001
            raise InpaintError(f"IOPaint returned a non-image response "
                               f"({r.headers.get('content-type')})", 502) from e
        out = out.convert("RGB")
        if out.size != image.size:
            raise InpaintError(f"IOPaint returned size {out.size}, expected {image.size}", 502)
        return out
