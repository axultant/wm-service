import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.inpaint import IOPaintClient  # noqa: E402
from app.main import create_app  # noqa: E402

OUT_DIR = ROOT / "tests" / "out"


@pytest.fixture
def roots(tmp_path):
    root = tmp_path / "photos"
    (root / "sub").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    return root, outside


@pytest.fixture
def test_logo(tmp_path):
    """Opaque pure-red 4:1 logo -> easy to locate by color."""
    p = tmp_path / "logo.png"
    Image.new("RGBA", (1000, 250), (255, 0, 0, 255)).save(p)
    return p


@pytest.fixture
def settings(roots, tmp_path, test_logo):
    root, _ = roots
    return Settings(
        allowed_roots=[root.resolve()],
        mock_inpaint=True,
        cache_dir=tmp_path / "cache",
        logo_path=test_logo,
        font_path=tmp_path / "missing.ttf",
    )


@pytest.fixture
def client(settings):
    from fastapi.testclient import TestClient

    iop = IOPaintClient("http://127.0.0.1:1", mock=True)
    return TestClient(create_app(settings, iop))
