import threading

import numpy as np
from PIL import Image, ImageOps
from sqlmodel import Session, select

from .config import LIBRARY_DIR
from .db import engine
from .models import Face, Photo

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - HEIC support is optional
    pass


class FaceDetector:
    def __init__(self) -> None:
        from insightface.app import FaceAnalysis

        self.app = FaceAnalysis(
            allowed_modules=["detection"],
            providers=["CPUExecutionProvider"],
        )
        self.app.prepare(ctx_id=0, det_size=(640, 640))

    def detect(self, path):
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            width, height = im.size
            arr = np.asarray(im)[:, :, ::-1]  # RGB -> BGR for InsightFace

        faces = self.app.get(arr)
        results = []
        for f in faces:
            x1, y1, x2, y2 = (float(v) for v in f.bbox)
            results.append(
                {
                    "x": max(0.0, x1),
                    "y": max(0.0, y1),
                    "w": max(0.0, x2 - x1),
                    "h": max(0.0, y2 - y1),
                    "confidence": float(getattr(f, "det_score", 0.0)),
                }
            )
        return width, height, results


_detector = None
_detector_lock = threading.Lock()


def get_detector() -> FaceDetector:
    global _detector
    with _detector_lock:
        if _detector is None:
            _detector = FaceDetector()
    return _detector


def process_pending() -> None:
    with Session(engine) as session:
        pending = session.exec(select(Photo).where(Photo.processed == False)).all()  # noqa: E712
        if not pending:
            return
        detector = get_detector()
        for photo in pending:
            path = LIBRARY_DIR / photo.filename
            try:
                width, height, faces = detector.detect(path)
            except Exception as exc:
                print(f"[detect] failed for {photo.filename}: {exc}")
                continue
            photo.width, photo.height = width, height
            for fd in faces:
                session.add(Face(photo_id=photo.id, **fd))
            photo.processed = True
            session.add(photo)
            session.commit()
            print(f"[detect] {photo.filename}: {len(faces)} face(s)")


def _worker_loop(stop_event: threading.Event, interval: float) -> None:
    while not stop_event.is_set():
        try:
            process_pending()
        except Exception as exc:  # pragma: no cover - keep worker alive
            print(f"[detect] worker error: {exc}")
        stop_event.wait(interval)


def start_worker(interval: float = 2.0):
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_worker_loop, args=(stop_event, interval), daemon=True
    )
    thread.start()
    return stop_event, thread
