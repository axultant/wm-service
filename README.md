# wm-service

A local-only web UI for manually masking old watermarks, removing them through a running IOPaint instance, and applying a configurable watermark template before downloading or replacing the original photo.

## Requirements

- Python 3.11 or newer
- A running IOPaint installation with the `lama` model
- Windows, macOS, or Linux

The application makes no external network calls and binds to localhost by default.

## Install

From this directory, create or activate a virtual environment and install the pinned dependencies:

```text
python -m venv .venv
.venv\\Scripts\\activate        # Windows
# source .venv/bin/activate      # macOS/Linux
python -m pip install -r requirements.txt
```

## Run

Start IOPaint first:

```text
iopaint start --model lama --port 8080
```

Then start this service in a second terminal:

```text
uvicorn app.main:app --host 127.0.0.1 --port 9000
```

Open `http://127.0.0.1:9000/` in a browser.

For UI development without IOPaint, use the mock client. It returns the submitted image unchanged:

```text
python -m app.main --mock-inpaint --port 9000
```

The service reads the IOPaint API schema captured from the installed version in `.iopaint_openapi.json` and calls `POST /api/v1/inpaint` with its JSON/base64 fields.

## Configuration

Edit `config.yaml`, or use environment overrides:

- `IOPAINT_URL` — IOPaint base URL, default `http://127.0.0.1:8080`
- `ALLOWED_ROOTS` — allowed photo roots, separated by `;` on Windows and `:` elsewhere
- `PORT` — service port, default `9000`
- `WM_MOCK_INPAINT=1` — enable mock inpainting
- `WM_CONFIG` — alternate YAML configuration path

Set at least one existing `allowed_roots` directory before browsing photos. Every requested path is resolved, symlinks are followed for validation, and paths outside those roots are rejected.

The `watermark` section contains the fixed template. The UI sliders are temporary per-render overrides; use **Copy as YAML** to make a chosen value part of the template. `backup: true` copies each original once to `.wm_backup/<relative path>` under its allowed root before replacement. Existing backups are never overwritten.

## Assets

- Replace `assets/logo.png` with an RGBA PNG. The logo is placed at the bottom right using width and margin fractions from the watermark configuration.
- Replace `assets/font.ttf` with the desired TrueType font. If it is missing, Pillow's bundled offline font is used.

Asset files are reloaded when their modification time changes, so the service does not need to be restarted after replacing them.

## Workflow

1. Expand a folder in the left sidebar and open a photo.
2. Paint the old watermark in the Remove tab. The mask is maintained at the image's natural resolution.
3. Press **Inpaint**. Repeated passes operate on the current result; use Undo/Redo and Original/Before-After to verify it.
4. Press **Send**. The Watermark tab renders a full-resolution PNG preview through the same watermark function used for export.
5. Adjust temporary logo/text values if needed, then use **Download** or confirm **Save to folder (replace original)**.

Download and Save use the original extension and format. JPEG output uses configured quality and 4:4:4 subsampling; ICC profiles and non-orientation EXIF are retained where Pillow can serialize them. Writes are atomic and never delete files.

The watermark template supports resolution-independent logo placement with `logo_anchor_x` and `logo_anchor_y` fractions from 0 (left/top) to 1 (right/bottom), plus `logo_enabled` and `text_enabled`. The browser's 3x3 Placement grid, sliders, and toggles apply to both single-photo and batch previews. **Save as default template** atomically writes `config.local.yaml`; it overrides matching values from `config.yaml` at startup. `config.yaml` is never modified.

Batch mode is available from the photo sidebar. Select individual photos or use Select all, Select none, Invert, Select not processed, Shift-click ranges, or Ctrl/Cmd+A. Choose replace (which keeps the existing `.wm_backup` first-copy rule) or copy (writes to `_watermarked`), confirm explicitly, and monitor progress. A server-side `.wm_state.json` registry records successful outputs with size and mtime verification; changed files no longer count as processed. `/api/restore` restores the first backup atomically without deleting it.


Run the test suite with:

```text
python -m pytest -q
```

Watermark samples are written to `tests/out/` for visual inspection.
