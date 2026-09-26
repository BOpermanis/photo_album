"""Perspective correction for "photo of a photo" scans.

The user marks four corners of the real photo inside the frame; those corners
are warped to the corners of a new rectangular image. An optional 90-degree
rotation fixes orientation. Corners are stored as fractions of the *rotated*
image so they line up with what the editor displays.
"""

import uuid
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageOps

from .config import LIBRARY_DIR

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - HEIC support is optional
    pass


def _load_rgb(path) -> Image.Image:
    with Image.open(path) as im:
        return ImageOps.exif_transpose(im).convert("RGB")


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """Order 4 points as top-left, top-right, bottom-right, bottom-left."""
    s = pts.sum(axis=1)
    diff = pts[:, 1] - pts[:, 0]  # y - x
    return np.array(
        [
            pts[np.argmin(s)],  # top-left  (small x + y)
            pts[np.argmin(diff)],  # top-right (small y - large x)
            pts[np.argmax(s)],  # bottom-right (large x + y)
            pts[np.argmax(diff)],  # bottom-left (large y - small x)
        ],
        dtype=np.float32,
    )


def auto_detect_quad(path) -> Optional[List[List[float]]]:
    """Best-guess quad of the photo within the frame, as [ [fx, fy], ... ].

    Returns fractions (0..1) of the EXIF-oriented original, ordered
    TL, TR, BR, BL, or None if no convincing quad is found.
    """
    im = _load_rgb(path)
    W, H = im.size
    arr = np.asarray(im)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

    scale = 1000.0 / max(W, H) if max(W, H) > 1000 else 1.0
    small = cv2.resize(gray, (max(1, int(W * scale)), max(1, int(H * scale))))
    blur = cv2.GaussianBlur(small, (5, 5), 0)
    edges = cv2.Canny(blur, 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8))

    contours, _ = cv2.findContours(
        edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    img_area = small.shape[0] * small.shape[1]
    best = None
    best_area = 0.0
    for c in contours:
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            area = cv2.contourArea(approx)
            if area > best_area and area > 0.2 * img_area:
                best_area, best = area, approx
    if best is None:
        return None

    pts = _order_corners(best.reshape(4, 2).astype(np.float32) / scale)
    return [[float(x / W), float(y / H)] for x, y in pts]


def _rotate(im: Image.Image, rotation: int) -> Image.Image:
    rotation %= 360
    if rotation == 0:
        return im
    # PIL rotates counter-clockwise; negate for clockwise degrees.
    return im.rotate(-rotation, expand=True)


def warp_and_save(
    original_path, rotation: int, points_frac: List[List[float]]
) -> Tuple[str, int, int]:
    """Rotate then perspective-warp the original; save a new library file.

    `points_frac` are 4 corners as fractions of the rotated image. Output
    dimensions come from the distance between the midpoints of opposite sides,
    so the selected shape's proportions are preserved without stretching.
    Returns (stored_name, width, height).
    """
    im = _rotate(_load_rgb(original_path), rotation)
    rw, rh = im.size
    arr = np.asarray(im)

    pts = np.array(
        [[fx * rw, fy * rh] for fx, fy in points_frac], dtype=np.float32
    )
    tl, tr, br, bl = _order_corners(pts)

    top_mid = (tl + tr) / 2.0
    bottom_mid = (bl + br) / 2.0
    left_mid = (tl + bl) / 2.0
    right_mid = (tr + br) / 2.0
    out_w = int(round(np.linalg.norm(right_mid - left_mid)))
    out_h = int(round(np.linalg.norm(bottom_mid - top_mid)))
    out_w = max(1, out_w)
    out_h = max(1, out_h)

    src = np.array([tl, tr, br, bl], dtype=np.float32)
    dst = np.array(
        [[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(
        arr, matrix, (out_w, out_h), flags=cv2.INTER_CUBIC
    )

    stored_name = f"warp_{uuid.uuid4().hex[:16]}.jpg"
    dest = LIBRARY_DIR / stored_name
    Image.fromarray(warped).save(dest, "JPEG", quality=95)
    return stored_name, out_w, out_h


def delete_file(filename: str) -> None:
    if not filename:
        return
    try:
        (LIBRARY_DIR / filename).unlink(missing_ok=True)
    except Exception:  # pragma: no cover - best-effort cleanup
        pass
