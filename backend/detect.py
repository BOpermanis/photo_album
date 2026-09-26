import threading

import numpy as np
from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - HEIC support is optional
    pass


class FaceDetector:
    def __init__(self) -> None:
        from insightface.app import FaceAnalysis

        self.app = FaceAnalysis(
            allowed_modules=["detection", "recognition"],
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
        embeddings = []
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
            emb = getattr(f, "normed_embedding", None)
            embeddings.append(
                np.asarray(emb, dtype=np.float32) if emb is not None else None
            )
        return width, height, results, embeddings

    def embed_photo(self, path, faces):
        """Compute embeddings for stored Face rows by IoU-matching detections."""
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            arr = np.asarray(im)[:, :, ::-1]
        detected = self.app.get(arr)
        out = {}
        for face in faces:
            best_emb = None
            best_iou = 0.0
            for d in detected:
                emb = getattr(d, "normed_embedding", None)
                if emb is None:
                    continue
                x1, y1, x2, y2 = (float(v) for v in d.bbox)
                iou = _iou(
                    (face.x, face.y, face.w, face.h),
                    (x1, y1, x2 - x1, y2 - y1),
                )
                if iou > best_iou:
                    best_iou, best_emb = iou, emb
            if best_emb is not None and best_iou >= 0.3:
                out[face.id] = np.asarray(best_emb, dtype=np.float32)
        return out


_detector = None
_detector_lock = threading.Lock()


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix1, iy1 = max(ax, bx), max(ay, by)
    ix2, iy2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def get_detector() -> FaceDetector:
    global _detector
    with _detector_lock:
        if _detector is None:
            _detector = FaceDetector()
    return _detector
