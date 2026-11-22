import io
import os

import numpy as np
import pytest
from PIL import Image, ImageCms, ImageOps

ORIENT = 0x0112
MAKE = 0x010F


def _quadrant_image(w=300, h=200):
    a = np.zeros((h, w, 3), np.uint8)
    a[: h // 2, : w // 2] = (255, 0, 0)
    a[: h // 2, w // 2:] = (0, 255, 0)
    a[h // 2:, : w // 2] = (0, 0, 255)
    a[h // 2:, w // 2:] = (255, 255, 0)
    return Image.fromarray(a)


def _make_jpeg(path, orientation=6):
    img = _quadrant_image()
    exif = Image.Exif()
    exif[ORIENT] = orientation
    exif[MAKE] = "TestCam"
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    img.save(path, "JPEG", quality=95, exif=exif.tobytes(), icc_profile=icc)
    return icc


def _get_png(client, path):
    r = client.get("/api/image", params={"path": str(path)})
    assert r.status_code == 200, r.text
    return r.content


def _save(client, path, png):
    return client.post("/api/save", data={"path": str(path)}, files={"image": ("x.png", png, "image/png")})


def test_save_jpeg_orientation_backup_atomic(client, roots):
    root, _ = roots
    f = root / "sub" / "Photo 1.JPG"
    icc = _make_jpeg(f)
    original_bytes = f.read_bytes()
    with Image.open(f) as o:
        upright = np.asarray(ImageOps.exif_transpose(o).convert("RGB")).astype(int)

    png = _get_png(client, f)
    shown = Image.open(io.BytesIO(png))
    assert shown.size == (200, 300)  # transposed space

    r = _save(client, f, png)
    assert r.status_code == 200, r.text
    res = r.json()
    backup = root / ".wm_backup" / "sub" / "Photo 1.JPG"
    assert res["path"] == str(f.resolve())
    assert res["backup"] == str(backup.resolve()) or os.path.samefile(res["backup"], backup)
    assert res["backup_created"] is True
    assert res["size"] == f.stat().st_size
    assert backup.read_bytes() == original_bytes

    # same name/extension, still JPEG, upright, Orientation absent or 1, other EXIF + ICC kept
    assert sorted(p.name for p in (root / "sub").iterdir()) == ["Photo 1.JPG"]  # no temp leftovers
    with Image.open(f) as s:
        assert s.format == "JPEG"
        assert s.size == (200, 300)
        ex = s.getexif()
        assert ex.get(ORIENT, 1) == 1
        assert ex.get(MAKE) == "TestCam"
        assert s.info.get("icc_profile") == icc
        raw = s.info.get("exif", b"")
        assert raw.count(b"\x01\x12") <= 1  # no duplicated orientation tag
        diff = np.abs(np.asarray(ImageOps.exif_transpose(s).convert("RGB")).astype(int) - upright).mean()
        assert diff < 6

    # second save: backup is NOT overwritten
    png2 = _get_png(client, f)
    r2 = _save(client, f, png2)
    assert r2.status_code == 200
    assert r2.json()["backup_created"] is False
    assert backup.read_bytes() == original_bytes


def test_save_atomic_failure_keeps_original(client, roots, monkeypatch):
    root, _ = roots
    f = root / "a.jpg"
    _make_jpeg(f, orientation=1)
    before = f.read_bytes()
    png = _get_png(client, f)

    import app.files as files_mod

    real_replace = os.replace

    def boom(src, dst):
        if os.path.abspath(dst) == os.path.abspath(f):
            raise OSError("disk full (simulated)")
        return real_replace(src, dst)

    monkeypatch.setattr(files_mod.os, "replace", boom)
    with pytest.raises(OSError):
        _save(client, f, png)
    assert f.read_bytes() == before
    assert not [p for p in root.iterdir() if p.name.startswith(".wm_tmp_")]


@pytest.mark.parametrize("ext,fmt,mode", [(".png", "PNG", "RGB"), (".png", "PNG", "RGBA"), (".webp", "WEBP", "RGB")])
def test_png_webp_keep_format(client, roots, ext, fmt, mode):
    root, _ = roots
    f = root / f"pic{ext}"
    Image.new(mode, (64, 48), (10, 20, 30, 200)[: len(mode)]).save(f, fmt)
    png = _get_png(client, f)
    r = _save(client, f, png)
    assert r.status_code == 200, r.text
    with Image.open(f) as s:
        assert s.format == fmt and s.size == (64, 48)
        assert ("A" in s.getbands()) == (mode == "RGBA")


def test_save_rejects_size_mismatch_outside_and_non_image(client, roots):
    root, outside = roots
    f = root / "a.jpg"
    _make_jpeg(f, orientation=6)
    bad = io.BytesIO()
    Image.new("RGB", (300, 200)).save(bad, "PNG")  # not transposed -> mismatch
    assert _save(client, f, bad.getvalue()).status_code == 409

    o = outside / "o.jpg"
    _make_jpeg(o)
    before = o.read_bytes()
    assert _save(client, o, _get_png_raw(o)).status_code == 403
    assert _save(client, str(root / ".." / "outside" / "o.jpg"), _get_png_raw(o)).status_code == 403
    assert o.read_bytes() == before

    t = root / "x.txt"
    t.write_text("x")
    assert _save(client, t, bad.getvalue()).status_code == 400
    assert not (outside.parent / ".wm_backup").exists()


def _get_png_raw(path):
    with Image.open(path) as im:
        b = io.BytesIO()
        ImageOps.exif_transpose(im).save(b, "PNG")
        return b.getvalue()


def test_export_matches_save_encoding(client, roots):
    root, _ = roots
    f = root / "e.jpeg"
    _make_jpeg(f)
    png = _get_png(client, f)
    r = client.post("/api/export", data={"path": str(f)}, files={"image": ("x.png", png, "image/png")})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert "e.jpeg" in r.headers["content-disposition"]
    s = _save(client, f, png)
    assert f.read_bytes() == r.content
    assert s.json()["size"] == len(r.content)
