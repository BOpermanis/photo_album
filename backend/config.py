from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LIBRARY_DIR = DATA_DIR / "library"
DB_PATH = DATA_DIR / "app.db"
FRONTEND_DIR = BASE_DIR / "frontend"

HOST = "0.0.0.0"
PORT = 8000

# Background worker pool. Each process loads its own detector, so RAM scales
# with WORKER_PROCESSES; keep it small. Override with PHOTO_WORKERS env.
WORKER_PROCESSES = int(os.environ.get("PHOTO_WORKERS", "2"))
WORKER_POLL_INTERVAL = 1.0
# A dedicated align-only worker (no detector model) keeps crops responsive even
# when every detection worker is busy; it polls faster since align is cheap.
INTERACTIVE_POLL_INTERVAL = 0.25

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}

# Face recognition (in-memory Annoy index of user-confirmed reference faces).
EMBED_DIM = 512
ANNOY_N_TREES = 10
# Cosine similarity threshold for surfacing a name suggestion (user confirms).
RECOGNITION_THRESHOLD = 0.35
ANNOY_PATH = DATA_DIR / "face_index.ann"

LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
