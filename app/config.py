"""Settings loader: config.yaml + environment overrides.

Env overrides:
    WM_CONFIG        path to the YAML file (default: <project>/config.yaml)
    IOPAINT_URL      IOPaint base URL
    ALLOWED_ROOTS    one or more root directories, separated by os.pathsep (';' on Windows, ':' elsewhere)
    PORT             port for `python -m app.main`
    WM_MOCK_INPAINT  "1" -> return the input unchanged instead of calling IOPaint
"""
from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .watermark import WMConfig, wm_config_from_dict

log = logging.getLogger("wm.config")

PROJECT_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    iopaint_url: str = "http://127.0.0.1:8080"
    allowed_roots: list[Path] = field(default_factory=list)
    host: str = "127.0.0.1"
    port: int = 9000
    mock_inpaint: bool = False
    inpaint_timeout: float = 300.0
    hd_strategy: str = "Crop"
    crop_margin: int = 128
    crop_trigger_size: int = 800
    default_dilate_px: int = 6
    backup: bool = True
    backup_dir_name: str = ".wm_backup"
    jpeg_quality: int = 95
    webp_quality: int = 95
    batch_workers: int = 2
    cache_dir: Path = PROJECT_DIR / ".cache"
    logo_path: Path = PROJECT_DIR / "assets" / "logo.png"
    font_path: Path = PROJECT_DIR / "assets" / "font.ttf"
    watermark: WMConfig = field(default_factory=WMConfig)
    config_file: Path | None = None


def _proj_path(p: str | os.PathLike) -> Path:
    path = Path(os.path.expanduser(str(p)))
    return path if path.is_absolute() else PROJECT_DIR / path


def _truthy(v: str | None) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def load_settings(config_file: str | os.PathLike | None = None, env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    path = Path(config_file or env.get("WM_CONFIG") or PROJECT_DIR / "config.yaml")
    data: dict[str, Any] = {}
    for candidate in (path, path.parent / "config.local.yaml"):
        if candidate.is_file():
            with open(candidate, "r", encoding="utf-8") as fh:
                part = yaml.safe_load(fh) or {}
            if isinstance(part, dict):
                data = {**data, **part}
                if isinstance(part.get("watermark"), dict):
                    data["watermark"] = {**(data.get("watermark") or {}), **part["watermark"]}

    s = Settings(config_file=path if path.is_file() else None)
    s.iopaint_url = str(data.get("iopaint_url", s.iopaint_url))
    s.host = str(data.get("host", s.host))
    s.port = int(data.get("port", s.port))
    s.mock_inpaint = bool(data.get("mock_inpaint", s.mock_inpaint))
    s.backup = bool(data.get("backup", s.backup))
    s.jpeg_quality = int(data.get("jpeg_quality", s.jpeg_quality))
    s.webp_quality = int(data.get("webp_quality", s.webp_quality))
    s.batch_workers = max(1, int(data.get("batch_workers", s.batch_workers)))
    if data.get("cache_dir"):
        s.cache_dir = _proj_path(data["cache_dir"])

    inp = data.get("inpaint") or {}
    s.inpaint_timeout = float(inp.get("timeout", s.inpaint_timeout))
    s.hd_strategy = str(inp.get("hd_strategy", s.hd_strategy))
    s.crop_margin = int(inp.get("crop_margin", s.crop_margin))
    s.crop_trigger_size = int(inp.get("crop_trigger_size", s.crop_trigger_size))
    s.default_dilate_px = int(inp.get("default_dilate_px", s.default_dilate_px))

    assets = data.get("assets") or {}
    if assets.get("logo"):
        s.logo_path = _proj_path(assets["logo"])
    if assets.get("font"):
        s.font_path = _proj_path(assets["font"])

    s.watermark = wm_config_from_dict(data.get("watermark") or {})

    roots = data.get("allowed_roots") or []
    if isinstance(roots, str):
        roots = [roots]

    # --- env overrides
    if env.get("IOPAINT_URL"):
        s.iopaint_url = env["IOPAINT_URL"]
    if env.get("ALLOWED_ROOTS"):
        roots = [r for r in env["ALLOWED_ROOTS"].split(os.pathsep) if r.strip()]
    if env.get("PORT"):
        s.port = int(env["PORT"])
    if _truthy(env.get("WM_MOCK_INPAINT")):
        s.mock_inpaint = True

    s.allowed_roots = []
    for r in roots:
        p = Path(os.path.expanduser(str(r)))
        if not p.is_dir():
            log.warning("Allowed root %s does not exist or is not a directory; skipped", p)
            continue
        s.allowed_roots.append(p.resolve())
    if not s.allowed_roots:
        log.warning("No valid allowed_roots configured: folder browsing is disabled. "
                    "Set allowed_roots in config.yaml or ALLOWED_ROOTS env var.")
    return s




def save_local_watermark(cfg: WMConfig) -> Path:
    path = PROJECT_DIR / "config.local.yaml"
    content = yaml.safe_dump({"watermark": {f.name: getattr(cfg, f.name) for f in fields(WMConfig)}}, sort_keys=False)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config_local_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
