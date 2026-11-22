"""Safe folder browsing, thumbnails, encoding and in-place save.

Every user-supplied path goes through `resolve_in_roots`, which calls
`Path.resolve()` (following symlinks) and then checks containment against the
resolved allowed roots. Nothing is ever deleted; files outside the roots are
never written.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageOps

log = logging.getLogger("wm.files")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
FORMAT_BY_EXT = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}
ORIENTATION_TAG = 0x0112
HIDDEN_DIRS = {".wm_backup", ".cache", "__pycache__"}

_STATE_LOCK = threading.RLock()

Image.MAX_IMAGE_PIXELS = 200_000_000  # allow big photos, still guard against bombs


class PathError(Exception):
    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.message = message
        self.status = status


# --------------------------------------------------------------------------- paths

def resolve_in_roots(raw: str | os.PathLike, roots: Iterable[Path], *, must_exist: bool = True) -> tuple[Path, Path]:
    """Resolve `raw` and return (resolved_path, root). Raise PathError if it escapes.

    Relative paths are interpreted relative to the first allowed root.
    """
    roots = [Path(r).resolve() for r in roots]
    if not roots:
        raise PathError("No allowed roots configured", 403)
    if raw is None or str(raw).strip() == "":
        raise PathError("Empty path", 400)
    if "\x00" in str(raw):
        raise PathError("Invalid path", 400)
    p = Path(os.path.expanduser(str(raw)))
    if not p.is_absolute():
        p = roots[0] / p
    try:
        resolved = p.resolve(strict=must_exist)
    except (OSError, RuntimeError):
        raise PathError("Path not found", 404)
    for root in roots:
        if resolved == root or resolved.is_relative_to(root):
            return resolved, root
    raise PathError("Path is outside the allowed roots", 403)


def is_image_file(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in IMAGE_EXTS


def resolve_image(raw: str, roots: Iterable[Path]) -> tuple[Path, Path]:
    path, root = resolve_in_roots(raw, roots)
    if not is_image_file(path):
        raise PathError("Not a supported image file (jpg/jpeg/png/webp)", 400)
    return path, root


def resolve_dir(raw: str, roots: Iterable[Path]) -> tuple[Path, Path]:
    path, root = resolve_in_roots(raw, roots)
    if not path.is_dir():
        raise PathError("Not a directory", 400)
    return path, root


_num_re = re.compile(r"(\d+)")


def natural_key(name: str):
    return [(0, int(t), "") if t.isdigit() else (1, 0, t.casefold()) for t in _num_re.split(name) if t != ""]


# --------------------------------------------------------------------------- listing

def list_folders(raw: str | None, roots: list[Path]) -> dict:
    if not raw:
        return {
            "path": None,
            "parent": None,
            "folders": [{"name": str(r), "path": str(r)} for r in roots],
        }
    path, root = resolve_dir(raw, roots)
    folders = []
    try:
        entries = list(os.scandir(path))
    except PermissionError:
        raise PathError("Permission denied", 403)
    for e in entries:
        if e.name.startswith(".") or e.name in HIDDEN_DIRS:
            continue
        try:
            if not e.is_dir():
                continue
            resolved, _ = resolve_in_roots(e.path, roots)  # drop symlinks escaping the roots
        except (PathError, OSError):
            continue
        folders.append({"name": e.name, "path": str(Path(e.path))})
    folders.sort(key=lambda f: natural_key(f["name"]))
    parent = None
    if path != root:
        parent = str(path.parent)
    return {"path": str(path), "parent": parent, "root": str(root), "folders": folders}


def list_images(raw: str, roots: list[Path]) -> dict:
    path, _ = resolve_dir(raw, roots)
    items = []
    for e in os.scandir(path):
        if e.name.startswith("."):
            continue
        if Path(e.name).suffix.lower() not in IMAGE_EXTS:
            continue
        try:
            if not e.is_file():
                continue
            resolve_in_roots(e.path, roots)
            st = e.stat()
        except (PathError, OSError):
            continue
        items.append({"name": e.name, "path": str(Path(e.path)), "size": st.st_size, "mtime": st.st_mtime})
    items.sort(key=lambda i: natural_key(i["name"]))
    return {"path": str(path), "images": items}


# --------------------------------------------------------------------------- images

def thumbnail(path: Path, w: int, cache_dir: Path) -> Path:
    w = max(32, min(int(w), 1024))
    st = path.stat()
    key = hashlib.sha1(f"{path}|{st.st_mtime_ns}|{st.st_size}|{w}".encode("utf-8")).hexdigest()
    out = cache_dir / "thumbs" / key[:2] / f"{key}.jpg"
    if out.is_file():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(path) as im:
        im.draft("RGB", (w, w))  # fast JPEG downscale on decode
        im = ImageOps.exif_transpose(im)
        im.thumbnail((w, w), Image.LANCZOS)
        if im.mode not in ("RGB", "L"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.getchannel("A"))
            im = bg
        fd, tmp = tempfile.mkstemp(dir=out.parent, suffix=".tmp")
        with os.fdopen(fd, "wb") as fh:
            im.convert("RGB").save(fh, "JPEG", quality=85)
        os.replace(tmp, out)
    return out


def load_transposed_png(path: Path) -> tuple[bytes, tuple[int, int]]:
    """Original image with EXIF orientation applied, as PNG (no ICC, raw pixel values)."""
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.getbands() or "transparency" in im.info else "RGB")
        buf = io.BytesIO()
        im.save(buf, "PNG", compress_level=1)
        return buf.getvalue(), im.size


def transposed_size(im: Image.Image) -> tuple[int, int]:
    o = im.getexif().get(ORIENTATION_TAG, 1)
    w, h = im.size
    return (h, w) if o in (5, 6, 7, 8) else (w, h)


@dataclass
class Encoded:
    data: bytes
    fmt: str
    ext: str


def encode_like_original(img: Image.Image, original: Path, jpeg_quality: int, webp_quality: int) -> Encoded:
    """Encode `img` (already orientation-corrected) in the original file's format.

    Keeps the ICC profile and other EXIF, drops the Orientation tag.
    """
    with Image.open(original) as orig:
        fmt = (orig.format or FORMAT_BY_EXT.get(original.suffix.lower(), "PNG")).upper()
        if fmt == "MPO":
            fmt = "JPEG"
        icc = orig.info.get("icc_profile")
        exif = orig.getexif()
        has_alpha = "A" in orig.getbands() or "transparency" in orig.info
        expected = transposed_size(orig)
    if img.size != expected:
        raise PathError(f"Image size {img.size} does not match the original {expected}", 409)

    exif_bytes = b""
    if exif:
        if ORIENTATION_TAG in exif:
            del exif[ORIENTATION_TAG]
        try:
            exif_bytes = exif.tobytes()
        except Exception:  # noqa: BLE001 - malformed EXIF: drop it rather than fail
            log.warning("Could not re-serialize EXIF of %s; EXIF dropped", original)
            exif_bytes = b""

    kw: dict = {}
    if icc:
        kw["icc_profile"] = icc
    if exif_bytes:
        kw["exif"] = exif_bytes

    if fmt == "JPEG":
        out = _flatten(img)
        kw.update(quality=jpeg_quality, subsampling=0, optimize=True)
    elif fmt == "WEBP":
        out = img.convert("RGBA") if has_alpha else _flatten(img)
        kw.update(quality=webp_quality, method=4)
    else:
        fmt = "PNG"
        out = img.convert("RGBA") if has_alpha else _flatten(img)
        kw.update(compress_level=6)
    buf = io.BytesIO()
    out.save(buf, fmt, **kw)
    return Encoded(buf.getvalue(), fmt, original.suffix)


def _flatten(img: Image.Image) -> Image.Image:
    if img.mode == "RGB":
        return img
    if "A" in img.getbands():
        bg = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    return img.convert("RGB")


def backup_path_for(path: Path, root: Path, dir_name: str = ".wm_backup") -> Path:
    return root / dir_name / path.relative_to(root)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".wm_tmp_", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _state_path(root: Path) -> Path:
    return root / ".wm_state.json"


def _read_state(root: Path) -> dict:
    try:
        with open(_state_path(root), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_state(root: Path, state: dict) -> None:
    _atomic_write_bytes(_state_path(root), json.dumps(state, indent=2, sort_keys=True).encode("utf-8"))


def state_for_folder(raw: str, roots: list[Path]) -> dict:
    folder, root = resolve_dir(raw, roots)
    with _STATE_LOCK:
        state = _read_state(root)
    result = {}
    for rel, entry in state.items():
        try:
            p = (root / rel).resolve()
            if p.parent != folder or not isinstance(entry, dict):
                continue
            st = p.stat()
            if st.st_size == entry.get("size") and st.st_mtime_ns == entry.get("mtime_ns"):
                result[str(p)] = entry
        except (OSError, ValueError):
            continue
    return {"path": str(folder), "statuses": result}


def update_state(path: Path, root: Path, *, template_hash: str, result_sha256: str, backup_path: str | None) -> dict:
    st = path.stat()
    entry = {
        "status": "processed", "template_hash": template_hash,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "size": st.st_size, "mtime_ns": st.st_mtime_ns,
        "sha256_of_result": result_sha256, "backup_path": backup_path,
    }
    rel = path.relative_to(root).as_posix()
    with _STATE_LOCK:
        state = _read_state(root)
        state[rel] = entry
        _write_state(root, state)
    return entry


def clear_state(path: Path, root: Path) -> None:
    rel = path.relative_to(root).as_posix()
    with _STATE_LOCK:
        state = _read_state(root)
        if rel in state:
            del state[rel]
            _write_state(root, state)


def processed_status(path: Path, root: Path) -> dict | None:
    with _STATE_LOCK:
        entry = _read_state(root).get(path.relative_to(root).as_posix())
    if not isinstance(entry, dict):
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    return entry if st.st_size == entry.get("size") and st.st_mtime_ns == entry.get("mtime_ns") else None


def restore_original(raw_path: str, roots: list[Path], backup_dir_name: str = ".wm_backup") -> dict:
    path, root = resolve_image(raw_path, roots)
    bp = backup_path_for(path, root, backup_dir_name)
    if not bp.is_file():
        raise PathError("No backup exists for this image", 404)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".wm_tmp_", suffix=path.suffix)
    try:
        os.close(fd)
        shutil.copy2(bp, tmp)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    clear_state(path, root)
    return {"path": str(path), "restored_from": str(bp)}


def save_copy(img: Image.Image, raw_path: str, roots: list[Path], *, jpeg_quality: int, webp_quality: int,
              output_dir_name: str = "_watermarked") -> dict:
    path, root = resolve_image(raw_path, roots)
    enc = encode_like_original(img, path, jpeg_quality, webp_quality)
    out = path.parent / output_dir_name / path.name
    resolve_in_roots(out, roots, must_exist=False)
    _atomic_write_bytes(out, enc.data)
    return {"path": str(out), "size": len(enc.data), "format": enc.fmt}


def save_replace(
    img: Image.Image,
    raw_path: str,
    roots: list[Path],
    *,
    backup: bool = True,
    backup_dir_name: str = ".wm_backup",
    jpeg_quality: int = 95,
    webp_quality: int = 95,
) -> dict:
    path, root = resolve_image(raw_path, roots)
    enc = encode_like_original(img, path, jpeg_quality, webp_quality)

    backup_file = None
    backup_created = False
    if backup:
        bp = backup_path_for(path, root, backup_dir_name)
        resolve_in_roots(bp, roots, must_exist=False)  # paranoia: stays inside root
        if not bp.exists():
            bp.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=bp.parent, prefix=".wm_tmp_", suffix=bp.suffix)
            os.close(fd)
            shutil.copy2(path, tmp)
            os.replace(tmp, bp)
            backup_created = True
        backup_file = str(bp)

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".wm_tmp_", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(enc.data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            shutil.copymode(path, tmp)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)  # only our own temp file
        raise
    return {
        "path": str(path),
        "size": len(enc.data),
        "format": enc.fmt,
        "backup": backup_file,
        "backup_created": backup_created,
    }
