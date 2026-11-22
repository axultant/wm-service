import numpy as np
import pytest
from PIL import Image

from app.watermark import WMConfig, apply_watermark, logo_box, wm_config_from_dict

from conftest import OUT_DIR

SIZES = [(450, 257), (1920, 1080), (4000, 3000), (1080, 1920), (3000, 800)]
CFG = WMConfig()


def red_bbox(img: Image.Image):
    a = np.asarray(img.convert("RGB")).astype(int)
    m = (a[..., 0] > 200) & (a[..., 1] < 60) & (a[..., 2] < 60)
    ys, xs = np.nonzero(m)
    return xs.min(), ys.min(), xs.max() + 1, ys.max() + 1


@pytest.mark.parametrize("W,H", SIZES)
def test_logo_geometry(W, H, test_logo):
    logo = Image.open(test_logo).convert("RGBA")
    src = Image.new("RGB", (W, H), (110, 130, 120))
    out = apply_watermark(src, CFG, logo, None)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out.convert("RGB").save(OUT_DIR / f"wm_{W}x{H}.png")

    assert out.size == (W, H) and out.mode == "RGBA"
    x0, y0, x1, y1 = red_bbox(out)
    bw, bh = x1 - x0, y1 - y0
    panorama = CFG.logo_width * W * logo.height / logo.width > CFG.logo_max_height * H
    if panorama:
        assert (W, H) == (3000, 800)
        assert bh <= CFG.logo_max_height * H + 1
        assert abs(bw - bh * logo.width / logo.height) <= 2  # aspect preserved
        assert bw < CFG.logo_width * W
    else:
        assert abs(bw / W - CFG.logo_width) <= 1 / W + 1e-9
    # bottom-right margins are a constant fraction of W (both axes)
    assert abs((W - x1) / W - CFG.logo_margin_x) <= 1 / W + 1e-9
    assert abs((H - y1) / W - CFG.logo_margin_y) <= 1 / W + 1e-9


@pytest.mark.parametrize("W,H", SIZES)
def test_text_present_and_input_untouched(W, H):
    src = Image.new("RGB", (W, H), (110, 130, 120))
    before = src.tobytes()
    out = apply_watermark(src, CFG, None, None)
    assert src.tobytes() == before
    a = np.asarray(out.convert("RGB")).astype(int)
    lighter = (a.sum(axis=2) > 360 + 30)
    # text covers all four quadrants (diagonal layer is big enough, no empty corners)
    h2, w2 = H // 2, W // 2
    for q in (lighter[:h2, :w2], lighter[:h2, w2:], lighter[h2:, :w2], lighter[h2:, w2:]):
        assert q.any()


def test_single_line_mode_and_min_font(test_logo):
    logo = Image.open(test_logo)
    cfg = wm_config_from_dict({"text_mode": "single_line", "text_font": 0.0001, "min_font_px": 12})
    src = Image.new("RGB", (800, 500), (40, 40, 40))
    out = apply_watermark(src, cfg, logo, None)
    out.convert("RGB").save(OUT_DIR / "wm_single_line.png")
    a = np.asarray(out.convert("RGB")).astype(int)
    lighter = a.sum(axis=2) > 120 + 30
    ys, _ = np.nonzero(lighter & ~((a[..., 0] > 200) & (a[..., 1] < 60)))
    assert ys.min() > 500 * 0.8  # only along the bottom
    assert ys.max() - ys.min() >= 8  # font at least ~min_font_px tall


def test_exif_orientation_is_applied():
    src = Image.new("RGB", (300, 200), (90, 90, 90))
    exif = src.getexif()
    exif[0x0112] = 6
    src.info["exif"] = exif.tobytes()
    src = Image.open(__import__("io").BytesIO(_jpeg(src, exif)))
    out = apply_watermark(src, CFG, None, None)
    assert out.size == (200, 300)


def _jpeg(img, exif):
    import io
    b = io.BytesIO()
    img.save(b, "JPEG", exif=exif.tobytes())
    return b.getvalue()


def test_zero_opacity_no_text():
    src = Image.new("RGB", (400, 300), (1, 2, 3))
    out = apply_watermark(src, wm_config_from_dict({"text_opacity": 0}), None, None)
    assert out.convert("RGB").tobytes() == src.tobytes()


def test_config_validation():
    with pytest.raises(ValueError):
        wm_config_from_dict({"text_mode": "bogus"})
    with pytest.raises(ValueError):
        wm_config_from_dict({"nope": 1})
    with pytest.raises(ValueError):
        wm_config_from_dict({"text_opacity": 2})


def test_logo_box_pixels():
    assert logo_box(1000, 1000, (400, 100), CFG) == (1000 - 20 - 210, 1000 - 20 - 52, 210, 52)
