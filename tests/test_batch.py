import io
import time

import numpy as np
from PIL import Image

from app.watermark import WMConfig, apply_watermark, clear_overlay_cache, logo_box


def png(im):
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def test_anchor_positions_are_resolution_independent(test_logo):
    logo = Image.open(test_logo).convert("RGBA")
    clear_overlay_cache()
    for anchor in ((0, 0), (.5, .5), (1, 1), (.3, .8)):
        fractions = []
        for size in ((450, 257), (1920, 1080), (4000, 3000), (1080, 1920), (3000, 800)):
            cfg = WMConfig(text_enabled=False, logo_anchor_x=anchor[0], logo_anchor_y=anchor[1])
            box = logo_box(*size, logo.size, cfg)
            out = apply_watermark(Image.new("RGB", size, (0, 0, 0)), cfg, logo)
            a = np.asarray(out.convert("RGB"))
            ys, xs = np.nonzero((a[..., 0] > 200) & (a[..., 1] < 60) & (a[..., 2] < 60))
            assert out.size == size
            fractions.append((xs.min() / size[0], ys.min() / size[1],
                              (xs.max() + 1) / size[0], (ys.max() + 1) / size[1]))
            expected = (box[0] / size[0], box[1] / size[1],
                        (box[0] + box[2]) / size[0], (box[1] + box[3]) / size[1])
            assert np.max(np.abs(np.asarray(fractions[-1]) - expected)) <= 1 / min(size) + 1e-9


def wait_job(client, job_id):
    for _ in range(200):
        result = client.get(f"/api/batch/{job_id}").json()
        if result["status"] != "running":
            return result
        time.sleep(.01)
    raise AssertionError("batch did not finish")


def test_batch_copy_failure_and_registry(client, roots):
    root, _ = roots
    good = root / "good.jpg"
    bad = root / "bad.jpg"
    original = Image.new("RGB", (80, 60), (20, 30, 40))
    original.save(good)
    bad.write_bytes(b"not an image")
    response = client.post("/api/batch/start", json={
        "paths": [str(good), str(bad)], "mode": "copy", "skip_processed": False,
        "overrides": {"logo_enabled": False, "text_enabled": False},
    })
    assert response.status_code == 200
    result = wait_job(client, response.json()["job_id"])
    assert result["status"] == "done" and result["ok"] == 1 and result["failed"] == 1
    assert Image.open(good).tobytes() == original.tobytes()
    assert (good.parent / "_watermarked" / good.name).is_file()

    state = client.get("/api/state", params={"path": str(root)}).json()
    assert str(good.resolve()) in state["statuses"]
    second = client.post("/api/batch/start", json={"paths": [str(good)], "mode": "copy"})
    assert second.status_code == 200
    assert wait_job(client, second.json()["job_id"])["skipped"] == 1


def test_batch_rejects_entire_request_outside_root(client, roots):
    root, outside = roots
    good = root / "good.jpg"
    Image.new("RGB", (20, 20)).save(good)
    other = outside / "other.jpg"
    Image.new("RGB", (20, 20)).save(other)
    response = client.post("/api/batch/start", json={"paths": [str(good), str(other)]})
    assert response.status_code == 403


def test_restore_clears_registry(client, roots):
    root, _ = roots
    path = root / "restore.jpg"
    original = Image.new("RGB", (32, 24), (1, 2, 3))
    original.save(path)
    original_bytes = path.read_bytes()
    replacement = Image.new("RGB", (32, 24), (200, 100, 50))
    saved = client.post("/api/save", data={"path": str(path)}, files={"image": ("x.png", png(replacement), "image/png")})
    assert saved.status_code == 200
    assert client.get("/api/state", params={"path": str(root)}).json()["statuses"]
    restored = client.post("/api/restore", json={"path": str(path)})
    assert restored.status_code == 200
    assert path.read_bytes() == original_bytes
    assert not client.get("/api/state", params={"path": str(root)}).json()["statuses"]
