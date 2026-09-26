# Family Photo Face-Tagging — Agent Guide

Local, single-user web app: upload phone photos over Wi-Fi, auto-detect faces, tag
them with names. FastAPI serves a vanilla-JS SPA; SQLite holds tags; InsightFace
(ONNX, CPU, GPU) does detection/recognition. See [README.md](README.md) for user setup,
WSL port forwarding, and the phone workflow.

## Setup & run

- Env: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
- Run: `python run.py` — serves `0.0.0.0:8000` and prints a QR code. No build step and
  no reload; restart the process to pick up backend changes.
- On WSL, the phone can't reach the WSL NAT IP: run [scripts/wsl-forward.ps1](scripts/wsl-forward.ps1)
  once in an **Administrator PowerShell on Windows** to forward port 8000 (re-run after a
  reboot, since the WSL IP changes). See [README.md](README.md) for details.
- First run downloads the InsightFace detection model automatically.
- There is **no test suite** and no linter config — verify changes by running the app.

## Architecture

Backend package `backend/`, launched by [run.py](run.py):

- [backend/app.py](backend/app.py) — every HTTP route plus dict-serialization helpers
  (`_face_dict`, `_photo_detail`). The SPA is mounted at `/` **last**, so declare new
  `/api/...` routes above that mount.
- [backend/worker.py](backend/worker.py) — multiprocessing pool + job handlers
  (`detect`, `align`, `rerun_detect`).
- [backend/jobs.py](backend/jobs.py) — persistent SQLite job queue; atomic claim,
  ordered by `priority DESC, id DESC`.
- [backend/db.py](backend/db.py) — engine, WAL pragmas, and additive migrations.
- [backend/models.py](backend/models.py) — SQLModel tables (`Album`, `Person`,
  `Photo`, `Face`, `Job`, `FaceIndexEntry`).
- [backend/detect.py](backend/detect.py) — InsightFace detector, per-process singleton.
- [backend/recognize.py](backend/recognize.py) — in-memory Annoy index of user-confirmed
  reference faces.
- [backend/warp.py](backend/warp.py) — perspective correction ("photo of a photo").
- [backend/export.py](backend/export.py) — album → PDF (reportlab).
- [backend/upload.py](backend/upload.py) — upload router; dedups by SHA-256 content hash.
- [backend/config.py](backend/config.py) — paths, thresholds, env overrides (`PHOTO_WORKERS`).
- Frontend is one file, [frontend/static/app.js](frontend/static/app.js): a hash-routed
  SPA (`#/photos`, `#/photo/:id`, `#/people`, `#/albums`, `#/jobs`, `#/upload`). No
  framework, no bundler.

## Conventions & pitfalls (non-obvious)

- **Never share a SQLite connection across processes.** Each worker builds its own engine
  via `db.make_engine()`; the pool uses `spawn` (not fork) to avoid sharing SQLite and
  onnxruntime state.
- **Detection/embedding run on the ALIGNED image when a corrected copy exists** (the
  original is the fallback); polygons are back-mapped into original space through the
  inverse homography. An `align` job only remaps existing polygons and never re-runs
  detection.
- **Face geometry** is stored as polygons (`poly_original` / `poly_aligned`, JSON of four
  `[x, y]` points) plus an axis-aligned bbox; the homography keeps the two a bijection.
- **Embeddings** are 512-d float32 stored as raw bytes — read with
  `np.frombuffer(..., np.float32)`, write with `.tobytes()`.
- **Migrations are additive**: guard new columns with `PRAGMA table_info` in
  `_run_migrations()`. Bump `SCHEMA_VERSION` only for a one-time data change (keyed off
  `PRAGMA user_version`), since that wipes and re-queues detections.
- **The frontend never polls** — photo detail updates stream via SSE at
  `/api/photos/{id}/stream`. Keep that endpoint a **sync** generator so Starlette runs it
  in a threadpool and `time.sleep` never blocks the event loop.
- **Escape all user text** with `esc()` before injecting HTML; destructive actions require
  the user to type "delete" (`confirmDelete`).
- **HEIC support is optional**: `pillow_heif.register_heif_opener()` is wrapped in
  try/except in every module that opens images — keep that pattern when adding one.

## Data

All runtime state lives in `data/` (`library/` = original uploads, `app.db` = tags,
`face_index.ann` = recognition index) and is gitignored. Never commit it or delete a
user's `data/`.
