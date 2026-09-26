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
    sw, sh = max(1, int(W * scale)), max(1, int(H * scale))
    small = cv2.resize(gray, (sw, sh))
    small = cv2.bilateralFilter(small, 9, 75, 75)
    img_area = sw * sh

    # Build several edge maps; real "photo of a photo" edges vary a lot, so we
    # union Canny (auto + fixed thresholds) with an Otsu-threshold boundary.
    med = float(np.median(small))
    lo = int(max(0, 0.66 * med))
    hi = int(min(255, 1.33 * med))
    edge_maps = [
        cv2.Canny(small, lo, hi),
        cv2.Canny(small, 50, 150),
        cv2.Canny(small, 30, 90),
    ]
    _, otsu = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    edge_maps.append(cv2.morphologyEx(otsu, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8)))

    kernel = np.ones((5, 5), np.uint8)
    candidates = []  # (area, quad_pts_smallscale)
    for edges in edge_maps:
        closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)
        closed = cv2.dilate(closed, kernel, iterations=1)
        contours, _ = cv2.findContours(
            closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        for c in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
            area = cv2.contourArea(c)
            if area < 0.10 * img_area:
                continue
            peri = cv2.arcLength(c, True)
            quad = None
            for eps in (0.02, 0.03, 0.05, 0.08):
                approx = cv2.approxPolyDP(c, eps * peri, True)
                if len(approx) == 4 and cv2.isContourConvex(approx):
                    quad = approx.reshape(4, 2).astype(np.float32)
                    break
            if quad is None:
                # Fall back to the minimum-area rectangle of a big contour.
                box = cv2.boxPoints(cv2.minAreaRect(c))
                rect_area = cv2.contourArea(box.astype(np.float32))
                if rect_area >= 0.25 * img_area and area >= 0.6 * rect_area:
                    quad = box.astype(np.float32)
            if quad is not None:
                candidates.append((cv2.contourArea(quad), quad))

    if not candidates:
        return None

    # Prefer the largest quad that still leaves a margin (i.e. not the whole
    # frame, which usually means it locked onto the image border, not the photo).
    candidates.sort(key=lambda t: t[0], reverse=True)
    best = None
    for area, quad in candidates:
        if area <= 0.98 * img_area:
            best = quad
            break
    if best is None:
        best = candidates[0][1]

    pts = _order_corners(best / scale)
    return [[float(np.clip(x / W, 0, 1)), float(np.clip(y / H, 0, 1))] for x, y in pts]


def _rotate(im: Image.Image, rotation: int) -> Image.Image:
    rotation %= 360
    if rotation == 0:
        return im
    # PIL rotates counter-clockwise; negate for clockwise degrees.
    return im.rotate(-rotation, expand=True)


def _rotation_matrix(w: int, h: int, rotation: int):
    """3x3 matrix mapping original EXIF pixel coords to rotated-image coords,
    plus the rotated (width, height). Rotation is clockwise, multiples of 90."""
    rotation %= 360
    if rotation == 0:
        m = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float64)
        return m, w, h
    if rotation == 90:
        m = np.array([[0, -1, h], [1, 0, 0], [0, 0, 1]], dtype=np.float64)
        return m, h, w
    if rotation == 180:
        m = np.array([[-1, 0, w], [0, -1, h], [0, 0, 1]], dtype=np.float64)
        return m, w, h
    # 270
    m = np.array([[0, 1, 0], [-1, 0, w], [0, 0, 1]], dtype=np.float64)
    return m, h, w


def _output_dims(tl, tr, br, bl) -> Tuple[int, int]:
    top_mid = (tl + tr) / 2.0
    bottom_mid = (bl + br) / 2.0
    left_mid = (tl + bl) / 2.0
    right_mid = (tr + br) / 2.0
    out_w = max(1, int(round(np.linalg.norm(right_mid - left_mid))))
    out_h = max(1, int(round(np.linalg.norm(bottom_mid - top_mid))))
    return out_w, out_h


def build_forward_matrix(
    original_size: Tuple[int, int], rotation: int, points_frac: List[List[float]]
):
    """Homography mapping original EXIF pixel coords -> aligned pixel coords.

    Composes the 90-degree rotation with the perspective correction so face
    polygons can be mapped between the two spaces without re-saving the image.
    Returns (matrix 3x3, out_w, out_h).
    """
    w, h = original_size
    rot, rw, rh = _rotation_matrix(w, h, rotation)
    pts = np.array([[fx * rw, fy * rh] for fx, fy in points_frac], dtype=np.float32)
    tl, tr, br, bl = _order_corners(pts)
    out_w, out_h = _output_dims(tl, tr, br, bl)
    src = np.array([tl, tr, br, bl], dtype=np.float32)
    dst = np.array(
        [[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]],
        dtype=np.float32,
    )
    persp = cv2.getPerspectiveTransform(src, dst).astype(np.float64)
    matrix = persp @ rot
    return matrix, out_w, out_h


def map_points(matrix, pts) -> List[List[float]]:
    """Apply a 3x3 homography to a list of [x, y] points."""
    arr = np.asarray(pts, dtype=np.float64).reshape(-1, 1, 2)
    out = cv2.perspectiveTransform(arr, np.asarray(matrix, dtype=np.float64))
    return [[float(x), float(y)] for x, y in out.reshape(-1, 2)]


def warp_and_save(
    original_path, rotation: int, points_frac: List[List[float]]
) -> Tuple[str, int, int, "np.ndarray"]:
    """Rotate then perspective-warp the original; save a new library file.

    `points_frac` are 4 corners as fractions of the rotated image. Output
    dimensions come from the distance between the midpoints of opposite sides,
    so the selected shape's proportions are preserved without stretching.
    Returns (stored_name, width, height, forward_matrix).
    """
    im = _load_rgb(original_path)
    arr = np.asarray(im)
    matrix, out_w, out_h = build_forward_matrix(im.size, rotation, points_frac)
    warped = cv2.warpPerspective(
        arr, matrix, (out_w, out_h), flags=cv2.INTER_CUBIC
    )

    stored_name = f"warp_{uuid.uuid4().hex[:16]}.jpg"
    dest = LIBRARY_DIR / stored_name
    Image.fromarray(warped).save(dest, "JPEG", quality=95)
    return stored_name, out_w, out_h, matrix


def delete_file(filename: str) -> None:
    if not filename:
        return
    try:
        (LIBRARY_DIR / filename).unlink(missing_ok=True)
    except Exception:  # pragma: no cover - best-effort cleanup
        pass
