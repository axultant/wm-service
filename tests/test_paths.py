import os

import pytest
from PIL import Image

from app.files import PathError, list_folders, list_images, resolve_dir, resolve_image, resolve_in_roots


def _img(p, size=(10, 10)):
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (1, 2, 3)).save(p)
    return p


def test_inside_ok(roots):
    root, _ = roots
    f = _img(root / "sub" / "a.jpg")
    p, r = resolve_image(str(f), [root])
    assert p == f.resolve() and r == root.resolve()
    assert resolve_dir(str(root / "sub"), [root])[0] == (root / "sub").resolve()


def test_relative_is_against_first_root(roots):
    root, _ = roots
    f = _img(root / "sub" / "a.png")
    assert resolve_image("sub/a.png", [root])[0] == f.resolve()


def test_traversal_rejected(roots):
    root, outside = roots
    _img(outside / "x.jpg")
    for raw in (str(root / ".." / "outside" / "x.jpg"), "../outside/x.jpg", str(root / "sub" / ".." / ".." / "outside")):
        with pytest.raises(PathError) as e:
            resolve_in_roots(raw, [root])
        assert e.value.status == 403


def test_absolute_outside_rejected(roots):
    root, outside = roots
    f = _img(outside / "x.jpg")
    with pytest.raises(PathError):
        resolve_image(str(f), [root])
    with pytest.raises(PathError):
        resolve_in_roots(os.path.abspath(os.sep), [root])


def test_prefix_sibling_rejected(roots, tmp_path):
    root, _ = roots
    sib = tmp_path / "photos_evil"
    f = _img(sib / "x.jpg")
    with pytest.raises(PathError):
        resolve_image(str(f), [root])


def _symlink(link, target, is_dir):
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"symlinks not available: {e}")


def test_symlink_escape_rejected(roots):
    root, outside = roots
    f = _img(outside / "secret.jpg")
    _symlink(root / "link.jpg", f, False)
    _symlink(root / "linkdir", outside, True)
    with pytest.raises(PathError):
        resolve_image(str(root / "link.jpg"), [root])
    with pytest.raises(PathError):
        resolve_image(str(root / "linkdir" / "secret.jpg"), [root])
    names = [x["name"] for x in list_folders(str(root), [root])["folders"]]
    assert "linkdir" not in names
    assert "link.jpg" not in [x["name"] for x in list_images(str(root), [root])["images"]]


def test_non_image_rejected(roots):
    root, _ = roots
    t = root / "notes.txt"
    t.write_text("hi")
    with pytest.raises(PathError) as e:
        resolve_image(str(t), [root])
    assert e.value.status == 400
    with pytest.raises(PathError):
        resolve_image(str(root / "sub"), [root])  # a directory


def test_missing_and_empty(roots):
    root, _ = roots
    with pytest.raises(PathError) as e:
        resolve_image(str(root / "nope.jpg"), [root])
    assert e.value.status == 404
    with pytest.raises(PathError):
        resolve_in_roots("", [root])


def test_listing_natural_sort_and_hidden(roots):
    root, _ = roots
    for n in ("img10.jpg", "img2.jpg", "IMG1.png", "c.webp", "skip.txt"):
        if n.endswith(".txt"):
            (root / n).write_text("x")
        else:
            _img(root / n)
    (root / ".wm_backup").mkdir()
    (root / "folder10").mkdir()
    (root / "folder9").mkdir()
    imgs = [i["name"] for i in list_images(str(root), [root])["images"]]
    assert imgs == ["c.webp", "IMG1.png", "img2.jpg", "img10.jpg"]
    folders = [f["name"] for f in list_folders(str(root), [root])["folders"]]
    assert folders == ["folder9", "folder10", "sub"]
    top = list_folders("", [root])
    assert top["folders"][0]["path"] == str(root.resolve())
