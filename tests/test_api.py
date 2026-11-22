import base64
import io
import json

import httpx
import numpy as np
from PIL import Image

from app.inpaint import IOPaintClient, InpaintError, dilate_mask


def _png(img):
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


def _pair(w=320, h=240):
    img = Image.new("RGB", (w, h), (50, 100, 150))
    mask = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    mask.paste((255, 255, 255, 255), (100, 100, 140, 120))
    return img, mask


def test_remove_mock_roundtrip(client):
    img, mask = _pair()
    r = client.post("/api/remove", data={"dilate_px": "6"},
                    files={"image": ("i.png", _png(img), "image/png"), "mask": ("m.png", _png(mask), "image/png")})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    out = Image.open(io.BytesIO(r.content))
    assert out.size == img.size
    assert out.convert("RGB").tobytes() == img.tobytes()  # mock returns the input unchanged


def test_remove_second_pass_on_result(client):
    img, mask = _pair()
    first = client.post("/api/remove", files={"image": ("i.png", _png(img)), "mask": ("m.png", _png(mask))}).content
    second = client.post("/api/remove", files={"image": ("i.png", first), "mask": ("m.png", _png(mask))})
    assert second.status_code == 200


def test_remove_errors(client):
    img, mask = _pair()
    empty = Image.new("L", img.size, 0)
    r = client.post("/api/remove", files={"image": ("i.png", _png(img)), "mask": ("m.png", _png(empty))})
    assert r.status_code == 400 and "empty" in r.json()["error"].lower()
    small = Image.new("L", (10, 10), 255)
    r = client.post("/api/remove", files={"image": ("i.png", _png(img)), "mask": ("m.png", _png(small))})
    assert r.status_code == 400 and "size" in r.json()["error"].lower()
    r = client.post("/api/remove", files={"image": ("i.png", b"garbage"), "mask": ("m.png", _png(mask))})
    assert r.status_code == 400


def test_dilate_mask():
    m = Image.new("L", (50, 50), 0)
    m.putpixel((25, 25), 255)
    d = dilate_mask(m, 6)
    assert d.getbbox() == (19, 19, 32, 32)
    assert dilate_mask(m, 0).getbbox() == (25, 25, 26, 26)
    assert set(np.unique(np.asarray(d))) <= {0, 255}


def test_client_uses_real_schema_fields():
    seen = {}
    result = Image.new("RGB", (64, 32), (9, 9, 9))

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=_png(result), headers={"content-type": "image/png"})

    c = IOPaintClient("http://iop:8080/", transport=httpx.MockTransport(handler))
    img = Image.new("RGB", (64, 32), (1, 1, 1))
    mask = Image.new("L", (64, 32), 255)
    out = c.inpaint(img, mask)
    assert out.tobytes() == result.tobytes()
    assert seen["url"] == "http://iop:8080/api/v1/inpaint"
    b = seen["body"]
    assert b["hd_strategy"] == "Crop"
    assert b["hd_strategy_crop_margin"] == 128
    assert b["hd_strategy_crop_trigger_size"] == 800
    assert Image.open(io.BytesIO(base64.b64decode(b["image"]))).size == (64, 32)
    assert Image.open(io.BytesIO(base64.b64decode(b["mask"]))).mode == "L"


def test_client_unreachable_gives_clear_error():
    c = IOPaintClient("http://127.0.0.1:1", timeout=2)
    try:
        c.inpaint(Image.new("RGB", (8, 8)), Image.new("L", (8, 8), 255))
        raise AssertionError("expected failure")
    except InpaintError as e:
        assert "not reachable" in e.message or "failed" in e.message
    assert c.health()["ok"] is False


def test_watermark_endpoint_formats(client):
    img = Image.new("RGB", (600, 400), (120, 120, 120))
    for fmt, ctype in (("png", "image/png"), ("jpeg", "image/jpeg"), ("webp", "image/webp")):
        r = client.post("/api/watermark", data={"fmt": fmt, "text_opacity": "0.5", "text_angle": "15"},
                        files={"image": ("i.png", _png(img))})
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == ctype
        assert Image.open(io.BytesIO(r.content)).size == (600, 400)
    r = client.post("/api/watermark", data={"text_opacity": "3"}, files={"image": ("i.png", _png(img))})
    assert r.status_code == 400


def test_browse_endpoints(client, roots):
    root, outside = roots
    Image.new("RGB", (800, 600)).save(root / "sub" / "a.jpg")
    assert client.get("/api/folders").json()["folders"][0]["path"] == str(root.resolve())
    assert [f["name"] for f in client.get("/api/folders", params={"path": str(root)}).json()["folders"]] == ["sub"]
    imgs = client.get("/api/images", params={"path": str(root / "sub")}).json()["images"]
    assert imgs[0]["name"] == "a.jpg" and imgs[0]["size"] > 0
    t = client.get("/api/thumb", params={"path": imgs[0]["path"], "w": 128})
    assert t.status_code == 200 and max(Image.open(io.BytesIO(t.content)).size) == 128
    t2 = client.get("/api/thumb", params={"path": imgs[0]["path"], "w": 128})
    assert t2.content == t.content
    assert client.get("/api/folders", params={"path": str(outside)}).status_code == 403
    assert client.get("/api/image", params={"path": str(root / ".." / "outside")}).status_code in (400, 403)
    assert client.get("/").status_code == 200
    cfg = client.get("/api/config").json()
    assert cfg["iopaint"]["mock"] is True and cfg["watermark"]["logo_width"] == 0.21
