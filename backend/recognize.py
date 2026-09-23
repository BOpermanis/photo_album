"""In-memory Annoy index of user-confirmed reference faces.

The index holds only faces a user has manually named. Embeddings for any
detected face live in a transient in-memory cache; reference embeddings are
persisted inside the Annoy `.ann` file. The mapping from each Annoy slot back
to its face/person lives in the `faceindexentry` SQLite table, so suggestions
survive a restart.
"""

import json
import threading

import numpy as np
from annoy import AnnoyIndex
from sqlmodel import Session, delete, select

from .config import (
    ANNOY_N_TREES,
    ANNOY_PATH,
    EMBED_DIM,
    RECOGNITION_THRESHOLD,
)
from .db import engine
from .models import FaceIndexEntry

_lock = threading.RLock()
_ann: AnnoyIndex | None = None
_order: list[int] = []  # face_id per Annoy item index
_ref: dict[int, tuple[int, np.ndarray]] = {}  # face_id -> (person_id, embedding)
_cache: dict[int, np.ndarray] = {}  # face_id -> embedding for any detected face


# ------------------------------ embedding cache ---------------------------
def cache_set(face_id: int, emb) -> None:
    with _lock:
        _cache[int(face_id)] = np.asarray(emb, dtype=np.float32)


def cache_get(face_id: int):
    with _lock:
        return _cache.get(int(face_id))


def cache_has(face_id: int) -> bool:
    with _lock:
        return int(face_id) in _cache


# ------------------------------ index lifecycle ---------------------------
def load() -> None:
    """Load the persisted Annoy index + mapping table into memory at startup."""
    global _ann, _order, _ref
    with _lock:
        _ann, _order, _ref = None, [], {}
        if not ANNOY_PATH.exists():
            return
        try:
            with Session(engine) as session:
                rows = session.exec(
                    select(FaceIndexEntry).order_by(FaceIndexEntry.item)
                ).all()
            if not rows and _migrate_legacy_json_locked():
                return
            if not rows:
                return
            ann = AnnoyIndex(EMBED_DIM, "angular")
            ann.load(str(ANNOY_PATH))
            for row in rows:
                vec = np.asarray(ann.get_item_vector(row.item), dtype=np.float32)
                _ref[row.face_id] = (row.person_id, vec)
                _order.append(row.face_id)
            _ann = ann
        except Exception as exc:  # pragma: no cover - corrupt/incompatible file
            print(f"[recognize] failed to load index: {exc}")
            _ann, _order, _ref = None, [], {}


def _migrate_legacy_json_locked() -> bool:
    """One-time import of the old JSON sidecar into the mapping table."""
    legacy = ANNOY_PATH.with_suffix(".json")
    if not legacy.exists():
        return False
    try:
        items = json.loads(legacy.read_text()).get("items", [])
        ann = AnnoyIndex(EMBED_DIM, "angular")
        ann.load(str(ANNOY_PATH))
        for i, it in enumerate(items):
            fid, pid = int(it["face_id"]), int(it["person_id"])
            _ref[fid] = (pid, np.asarray(ann.get_item_vector(i), dtype=np.float32))
        _rebuild_locked()
        legacy.unlink(missing_ok=True)
        print(f"[recognize] migrated {len(items)} entries from JSON to SQLite")
        return True
    except Exception as exc:  # pragma: no cover - best-effort migration
        print(f"[recognize] legacy migration failed: {exc}")
        _ref.clear()
        return False


def _rebuild_locked() -> None:
    global _ann, _order
    with Session(engine) as session:
        session.exec(delete(FaceIndexEntry))
        if not _ref:
            session.commit()
            _ann, _order = None, []
            try:
                ANNOY_PATH.unlink(missing_ok=True)
            except Exception:
                pass
            return
        ann = AnnoyIndex(EMBED_DIM, "angular")
        order: list[int] = []
        for i, (fid, (pid, vec)) in enumerate(_ref.items()):
            ann.add_item(i, vec.tolist())
            order.append(fid)
            session.add(FaceIndexEntry(item=i, face_id=fid, person_id=pid))
        ann.build(ANNOY_N_TREES)
        ann.save(str(ANNOY_PATH))
        session.commit()
        _ann, _order = ann, order


def add(face_id: int, person_id: int, emb) -> None:
    with _lock:
        _ref[int(face_id)] = (int(person_id), np.asarray(emb, dtype=np.float32))
        _rebuild_locked()


def remove(face_id: int) -> None:
    with _lock:
        if _ref.pop(int(face_id), None) is not None:
            _rebuild_locked()


def query(emb):
    """Return {'person_id', 'score'} for the closest reference, or None."""
    with _lock:
        if _ann is None or not _order:
            return None
        ids, dists = _ann.get_nns_by_vector(
            np.asarray(emb, dtype=np.float32).tolist(), 1, include_distances=True
        )
        if not ids:
            return None
        # angular distance d = sqrt(2*(1 - cos)) -> cos = 1 - d^2/2
        cos = 1.0 - (dists[0] ** 2) / 2.0
        if cos < RECOGNITION_THRESHOLD:
            return None
        person_id = _ref[_order[ids[0]]][0]
        return {"person_id": person_id, "score": float(cos)}
