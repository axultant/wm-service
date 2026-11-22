"""Watermark template rendering.

`apply_watermark` is a pure function: no file or network I/O. Assets (logo image,
font bytes) are passed in by the caller. All geometry is expressed as fractions of
the image width W (except `logo_max_height`, a fraction of H) and converted to
pixels at render time, so the result looks identical at any resolution.
"""
from __future__ import annotations

import io
import logging
import math
from collections import OrderedDict
from dataclasses import dataclass, fields, replace
from typing import Any, Mapping

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

log = logging.getLogger("wm.watermark")

TEXT_MODES = ("diagonal_tiled", "single_line")


@dataclass(frozen=True)
class WMConfig:
    logo_width: float = 0.21       # of W
    logo_margin_x: float = 0.02    # of W
    logo_margin_y: float = 0.02    # of W (not H, so margins look equal)
    logo_max_height: float = 0.12  # of H, ceiling for panoramas
    logo_enabled: bool = True
    text_enabled: bool = True
    logo_anchor_x: float = 1.0
    logo_anchor_y: float = 1.0
    text_font: float = 0.035       # of W
    text_gap_x: float = 0.10       # of W
    text_step_y: float = 0.13      # of W
    text_opacity: float = 0.28     # 0..1
    text_angle: float = 30.0       # degrees, counter-clockwise
    text_mode: str = "diagonal_tiled"  # or "single_line"
    min_font_px: int = 10
    company_text: str = "COMPANY NAME"

    def validate(self) -> "WMConfig":
        if not 0 < self.logo_width <= 1:
            raise ValueError("logo_width must be in (0, 1]")
        if not 0 < self.logo_max_height <= 1:
            raise ValueError("logo_max_height must be in (0, 1]")
        if not 0 <= self.logo_anchor_x <= 1 or not 0 <= self.logo_anchor_y <= 1:
            raise ValueError("logo anchors must be in [0, 1]")
        if not 0 <= self.text_opacity <= 1:
            raise ValueError("text_opacity must be in [0, 1]")
        if self.text_font <= 0 or self.min_font_px < 1:
            raise ValueError("text_font and min_font_px must be positive")
        if self.text_mode not in TEXT_MODES:
            raise ValueError(f"text_mode must be one of {TEXT_MODES}")
        return self


def wm_config_from_dict(data: Mapping[str, Any] | None, base: WMConfig | None = None) -> WMConfig:
    """Build a WMConfig from a mapping (e.g. YAML), coercing to the field types."""
    base = base or WMConfig()
    known = {f.name for f in fields(WMConfig)}
    kwargs: dict[str, Any] = {}
    for key, value in (data or {}).items():
        if key not in known:
            raise ValueError(f"Unknown watermark option: {key!r}")
        if value is None:
            continue
        if key in ("logo_anchor_x", "logo_anchor_y"):
            value = min(1.0, max(0.0, float(value)))
        if isinstance(getattr(base, key), bool) and isinstance(value, str):
            value = value.strip().lower() in ("1", "true", "yes", "on")
        kwargs[key] = type(getattr(base, key))(value)
    return replace(base, **kwargs).validate()


# --------------------------------------------------------------------------- helpers

def _load_font(font_bytes: bytes | None, size: int) -> ImageFont.FreeTypeFont:
    if font_bytes:
        return ImageFont.truetype(io.BytesIO(font_bytes), size)
    # Pillow's bundled open font (Aileron); works offline.
    return ImageFont.load_default(size=size)


def logo_box(W: int, H: int, logo_size: tuple[int, int], cfg: WMConfig) -> tuple[int, int, int, int]:
    """Return (x, y, w, h) of the logo in pixels for an image of size W x H."""
    lw, lh = logo_size
    tw = max(1, round(cfg.logo_width * W))
    th = max(1, round(tw * lh / lw))
    max_h = cfg.logo_max_height * H
    if th > max_h:  # panorama ceiling
        th = max(1, int(max_h))
        tw = max(1, round(th * lw / lh))
    mx = round(cfg.logo_margin_x * W)
    my = round(cfg.logo_margin_y * W)
    x = round(mx + cfg.logo_anchor_x * (W - tw - 2 * mx))
    y = round(my + cfg.logo_anchor_y * (H - th - 2 * my))
    return x, y, tw, th


def _compose_text_layer(fill: Image.Image, full: Image.Image, alpha: int) -> Image.Image:
    """fill = glyph mask, full = glyph+stroke mask -> RGBA layer.

    White fill at `alpha`, dark stroke at `alpha / 3`.
    """
    stroke_only = ImageChops.subtract(full, fill)
    a_fill = fill.point(lambda v: v * alpha // 255)
    a_stroke = stroke_only.point(lambda v: v * (alpha // 3) // 255)
    fill_layer = Image.merge("RGBA", (fill, fill, fill, a_fill))
    stroke_layer = Image.merge("RGBA", (Image.new("L", fill.size, 0),) * 3 + (a_stroke,))
    return Image.alpha_composite(stroke_layer, fill_layer)


def _text_layer(W: int, H: int, cfg: WMConfig, font_bytes: bytes | None, x_limit: int) -> Image.Image | None:
    if not cfg.text_enabled:
        return None
    alpha = round(255 * min(1.0, max(0.0, cfg.text_opacity)))
    text = cfg.company_text
    if alpha == 0 or not text.strip():
        return None

    size = max(cfg.min_font_px, round(cfg.text_font * W))
    font = _load_font(font_bytes, size)
    sw = max(1, round(size * 0.04))
    l, t, r, b = font.getbbox(text, stroke_width=sw)
    tw, th = r - l, b - t
    gap = max(1, round(cfg.text_gap_x * W))
    step_x = tw + gap

    def draw_both(df: ImageDraw.ImageDraw, du: ImageDraw.ImageDraw, x: float, y: float) -> None:
        pos = (x - l, y - t)
        du.text(pos, text, font=font, fill=255, stroke_width=sw, stroke_fill=255)
        df.text(pos, text, font=font, fill=255)

    if cfg.text_mode == "single_line":
        fill = Image.new("L", (W, H), 0)
        full = Image.new("L", (W, H), 0)
        df, du = ImageDraw.Draw(fill), ImageDraw.Draw(full)
        mx = round(cfg.logo_margin_x * W)
        y = H - round(cfg.logo_margin_y * W) - th
        span = max(0, x_limit - mx)
        n = max(1, (span + gap) // step_x)
        total = n * step_x - gap
        x = mx + (span - total) / 2
        for _ in range(n):
            draw_both(df, du, x, y)
            x += step_x
        return _compose_text_layer(fill, full, alpha)

    # diagonal_tiled: draw on a square layer the size of the image diagonal so the
    # rotated corners are never empty, rotate, then center-crop to W x H.
    D = math.ceil(math.hypot(W, H)) + 2
    fill = Image.new("L", (D, D), 0)
    full = Image.new("L", (D, D), 0)
    df, du = ImageDraw.Draw(fill), ImageDraw.Draw(full)
    step_y = max(th + 1, round(cfg.text_step_y * W))
    y = (D % step_y) / 2 - step_y
    row = 0
    while y < D:
        x = -step_x + (step_x // 2 if row % 2 else 0)  # staggered rows
        while x < D:
            draw_both(df, du, x, y)
            x += step_x
        y += step_y
        row += 1
    if cfg.text_angle % 360:
        fill = fill.rotate(cfg.text_angle, resample=Image.BICUBIC)
        full = full.rotate(cfg.text_angle, resample=Image.BICUBIC)
    left, top = (D - W) // 2, (D - H) // 2
    box = (left, top, left + W, top + H)
    return _compose_text_layer(fill.crop(box), full.crop(box), alpha)


# --------------------------------------------------------------------------- public

_OVERLAY_CACHE: OrderedDict[tuple, Image.Image] = OrderedDict()
_OVERLAY_CACHE_SIZE = 3


def _cfg_hash(cfg: WMConfig) -> tuple:
    return tuple(getattr(cfg, f.name) for f in fields(WMConfig))


def build_overlay(
    size: tuple[int, int],
    cfg: WMConfig,
    logo: Image.Image | None = None,
    font_bytes: bytes | None = None,
) -> Image.Image:
    """Build a transparent RGBA overlay for one image size and template."""
    W, H = size
    logo_key = (logo.size, logo.mode, logo.tobytes()) if logo is not None and cfg.logo_enabled else None
    font_key = font_bytes if cfg.text_enabled else None
    key = (W, H, _cfg_hash(cfg), logo_key, font_key)
    cached = _OVERLAY_CACHE.get(key)
    if cached is not None:
        _OVERLAY_CACHE.move_to_end(key)
        return cached.copy()

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    box = logo_box(W, H, logo.size, cfg) if logo is not None and cfg.logo_enabled else None
    gap = max(1, round(cfg.text_gap_x * W))
    x_limit = (box[0] - gap // 2) if box else W - round(cfg.logo_margin_x * W)
    layer = _text_layer(W, H, cfg, font_bytes, x_limit)
    if layer is not None:
        overlay = Image.alpha_composite(overlay, layer)
    if box is not None:
        x, y, w, h = box
        src = logo.convert("RGBA")
        if src.width < w:
            log.warning("Logo source is %dpx wide but target is %dpx; it will be upscaled "
                        "(provide a larger logo.png for best quality)", src.width, w)
        resized = src.resize((w, h), Image.LANCZOS)
        overlay.alpha_composite(resized, (x, y))

    _OVERLAY_CACHE[key] = overlay.copy()
    _OVERLAY_CACHE.move_to_end(key)
    while len(_OVERLAY_CACHE) > _OVERLAY_CACHE_SIZE:
        _OVERLAY_CACHE.popitem(last=False)
    return overlay


def clear_overlay_cache() -> None:
    _OVERLAY_CACHE.clear()


def apply_watermark(
    img: Image.Image,
    cfg: WMConfig,
    logo: Image.Image | None = None,
    font_bytes: bytes | None = None,
) -> Image.Image:
    """Return a new RGBA image with the template applied. The input is not modified."""
    base = ImageOps.exif_transpose(img).convert("RGBA")
    overlay = build_overlay(base.size, cfg, logo, font_bytes)
    return Image.alpha_composite(base, overlay)
