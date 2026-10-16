"""Apparent-age estimation for face crops.

Uses a ViT classifier trained on FairFace (nateraw/vit-age-classifier, fetched
from the Hugging Face hub on first use). InsightFace's bundled genderage model
proved useless on old family scans (mean error ~15 years; babies came out as
24-year-olds), whereas this one tracks children well and its nine age-band
probabilities yield a per-face uncertainty that is roughly calibrated.

Loaded once per process, only by the dedicated `age` worker lane.
"""

import os
import threading

import numpy as np
from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - HEIC support is optional
    pass

MODEL_NAME = "nateraw/vit-age-classifier"
# Representative ages of the bands 0-2, 3-9, 10-19, ..., 60-69, 70+.
_BAND_AGES = np.array([1, 6, 14.5, 24.5, 34.5, 44.5, 54.5, 64.5, 75], dtype=np.float64)
# Context around the detector box; FairFace crops include hair and chin.
_PAD = 0.4
_MIN_SD = 1.0


class AgeEstimator:
    def __init__(self) -> None:
        import torch
        from transformers import ViTForImageClassification, ViTImageProcessor

        torch.set_num_threads(max(1, min(4, (os.cpu_count() or 2) // 2)))
        self._torch = torch
        self.processor = ViTImageProcessor.from_pretrained(MODEL_NAME)
        self.model = ViTForImageClassification.from_pretrained(MODEL_NAME).eval()

    def estimate(self, image: Image.Image, boxes) -> list[tuple[float, float]]:
        """(mean age, sd) for each (x1, y1, x2, y2) box in `image`."""
        if not boxes:
            return []
        crops = []
        for x1, y1, x2, y2 in boxes:
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            s = max(x2 - x1, y2 - y1) * (1 + _PAD)
            crops.append(
                image.crop((int(cx - s / 2), int(cy - s / 2), int(cx + s / 2), int(cy + s / 2)))
            )
        with self._torch.no_grad():
            inputs = self.processor(crops, return_tensors="pt")
            probs = self.model(**inputs).logits.softmax(-1).numpy().astype(np.float64)
        means = probs @ _BAND_AGES
        sds = np.sqrt((probs * (_BAND_AGES[None, :] - means[:, None]) ** 2).sum(axis=1))
        return [(float(m), float(max(s, _MIN_SD))) for m, s in zip(means, sds)]


def open_rgb(path) -> Image.Image:
    with Image.open(path) as im:
        return ImageOps.exif_transpose(im).convert("RGB")


_estimator = None
_lock = threading.Lock()


def get_estimator() -> AgeEstimator:
    global _estimator
    with _lock:
        if _estimator is None:
            _estimator = AgeEstimator()
    return _estimator
