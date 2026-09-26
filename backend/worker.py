"""Multiprocessing worker pool that drains the persistent job queue.

Each child process owns its own SQLite engine and its own face detector (model
load is per-process). Jobs are claimed atomically from the queue so no two
workers touch the same row. Handlers keep detection/embedding on the ORIGINAL
image and map polygons into aligned space via the alignment homography, so an
`align` job never re-runs detection.
"""

import json
import multiprocessing as mp
import os

import numpy as np
from sqlmodel import Session, select

from . import db, jobs, warp
from .config import (
    INTERACTIVE_POLL_INTERVAL,
    LIBRARY_DIR,
    WORKER_POLL_INTERVAL,
    WORKER_PROCESSES,
)
from .models import Face, Photo


# ------------------------------ geometry helpers --------------------------
def _rect_poly(x: float, y: float, w: float, h: float):
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


def _bbox(poly):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    x, y = min(xs), min(ys)
    return x, y, max(xs) - x, max(ys) - y


def _forward_matrix(photo: Photo):
    """Homography original -> aligned from the photo's stored transform, or None."""
    if not photo.edit_params or not photo.original_width:
        return None
    params = json.loads(photo.edit_params)
    matrix, _, _ = warp.build_forward_matrix(
        (photo.original_width, photo.original_height),
        params["rotation"],
        params["points"],
    )
    return matrix


# ------------------------------- job handlers -----------------------------
def handle_detect(engine, detector, photo_id: int) -> None:
    """Detect + embed on the ORIGINAL; store polygons in original space and,
    when an aligned copy already exists, map them into aligned space too."""
    with Session(engine) as session:
        photo = session.get(Photo, photo_id)
        if photo is None:
            return
        original_path = LIBRARY_DIR / photo.filename

    width, height, results, embeddings = detector.detect(original_path)

    with Session(engine) as session:
        photo = session.get(Photo, photo_id)
        if photo is None:
            return
        for old in session.exec(select(Face).where(Face.photo_id == photo_id)).all():
            session.delete(old)
        photo.original_width, photo.original_height = width, height
        if not photo.edited_filename:
            photo.width, photo.height = width, height
        matrix = _forward_matrix(photo) if photo.edited_filename else None
        for fd, emb in zip(results, embeddings):
            poly_o = _rect_poly(fd["x"], fd["y"], fd["w"], fd["h"])
            poly_a = json.dumps(warp.map_points(matrix, poly_o)) if matrix is not None else ""
            session.add(
                Face(
                    photo_id=photo_id,
                    x=fd["x"], y=fd["y"], w=fd["w"], h=fd["h"],
                    confidence=fd["confidence"],
                    poly_original=json.dumps(poly_o),
                    poly_aligned=poly_a,
                    embedding=emb.tobytes() if emb is not None else None,
                )
            )
        photo.processed = True
        session.add(photo)
        session.commit()
        count = len(results)
    print(f"[worker] detect photo={photo_id}: {count} face(s)")


def handle_align(engine, detector, photo_id: int) -> None:
    """Generate the aligned image and remap existing face polygons through the
    homography. Detection is NOT re-run and embeddings are untouched."""
    with Session(engine) as session:
        photo = session.get(Photo, photo_id)
        if photo is None or not photo.edit_params:
            return
        params = json.loads(photo.edit_params)
        original_path = LIBRARY_DIR / photo.filename
        old_edited = photo.edited_filename

    stored_name, out_w, out_h, matrix = warp.warp_and_save(
        original_path, params["rotation"], params["points"]
    )

    with Session(engine) as session:
        photo = session.get(Photo, photo_id)
        if photo is None:
            warp.delete_file(stored_name)
            return
        photo.edited_filename = stored_name
        photo.width, photo.height = out_w, out_h
        for face in session.exec(select(Face).where(Face.photo_id == photo_id)).all():
            poly_o = (
                json.loads(face.poly_original)
                if face.poly_original
                else _rect_poly(face.x, face.y, face.w, face.h)
            )
            face.poly_aligned = json.dumps(warp.map_points(matrix, poly_o))
            session.add(face)
        session.add(photo)
        session.commit()

    if old_edited and old_edited != stored_name:
        warp.delete_file(old_edited)
    print(f"[worker] align photo={photo_id} -> {stored_name}")


def handle_rerun_detect(engine, detector, photo_id: int) -> None:
    """User-requested re-detection: run on the aligned image when present and
    back-map polygons into original space; otherwise behave like detect."""
    with Session(engine) as session:
        photo = session.get(Photo, photo_id)
        if photo is None:
            return
        display_path = LIBRARY_DIR / photo.display_filename
        aligned = bool(photo.edited_filename)
        matrix = _forward_matrix(photo) if aligned else None

    width, height, results, embeddings = detector.detect(display_path)
    inverse = np.linalg.inv(matrix) if matrix is not None else None

    with Session(engine) as session:
        photo = session.get(Photo, photo_id)
        if photo is None:
            return
        for old in session.exec(select(Face).where(Face.photo_id == photo_id)).all():
            session.delete(old)
        if aligned:
            photo.width, photo.height = width, height
        else:
            photo.original_width, photo.original_height = width, height
            photo.width, photo.height = width, height
        for fd, emb in zip(results, embeddings):
            rect = _rect_poly(fd["x"], fd["y"], fd["w"], fd["h"])
            if aligned and inverse is not None:
                poly_a = rect
                poly_o = warp.map_points(inverse, rect)
            else:
                poly_o = rect
                poly_a = None
            x, y, w, h = _bbox(poly_o)
            session.add(
                Face(
                    photo_id=photo_id,
                    x=x, y=y, w=w, h=h,
                    confidence=fd["confidence"],
                    poly_original=json.dumps(poly_o),
                    poly_aligned=json.dumps(poly_a) if poly_a is not None else "",
                    embedding=emb.tobytes() if emb is not None else None,
                )
            )
        photo.processed = True
        session.add(photo)
        session.commit()
        count = len(results)
    print(f"[worker] rerun_detect photo={photo_id}: {count} face(s)")


_HANDLERS = {
    "detect": handle_detect,
    "align": handle_align,
    "rerun_detect": handle_rerun_detect,
}


# ------------------------------- pool plumbing ----------------------------
def _worker_main(stop_event, kinds=None, load_detector=True, poll=WORKER_POLL_INTERVAL) -> None:
    engine = db.make_engine()
    detector = None
    if load_detector:
        from .detect import get_detector

        detector = get_detector()
    pid = os.getpid()
    lane = "+".join(kinds) if kinds else "all"
    print(f"[worker {pid}] ready ({lane})")
    while not stop_event.is_set():
        with Session(engine) as session:
            claimed = jobs.claim_next(session, pid, kinds)
        if claimed is None:
            stop_event.wait(poll)
            continue
        job_id, target_photo, kind, _payload = claimed
        try:
            handler = _HANDLERS.get(kind)
            if handler is None:
                raise ValueError(f"unknown job kind: {kind}")
            handler(engine, detector, target_photo)
            with Session(engine) as session:
                jobs.mark(session, job_id, "done")
        except Exception as exc:  # pragma: no cover - worker keeps running
            message = f"{type(exc).__name__}: {exc}"
            with Session(engine) as session:
                jobs.mark(session, job_id, "failed", error=message)
            print(f"[worker {pid}] {kind} photo={target_photo} FAILED: {message}")


def start_pool():
    """Spawn the worker processes. Spawn (not fork) avoids sharing SQLite and
    onnxruntime state across the process boundary.

    Alongside the detection workers we start one dedicated align-only lane. An
    `align` job just warps the image and remaps polygons (no detector model),
    so this lightweight worker lets a crop complete immediately instead of
    waiting behind slow detections that occupy every general worker.
    """
    ctx = mp.get_context("spawn")
    stop_event = ctx.Event()
    procs = []
    for _ in range(max(1, WORKER_PROCESSES)):
        proc = ctx.Process(target=_worker_main, args=(stop_event,), daemon=True)
        proc.start()
        procs.append(proc)
    interactive = ctx.Process(
        target=_worker_main,
        args=(stop_event,),
        kwargs={
            "kinds": ("align",),
            "load_detector": False,
            "poll": INTERACTIVE_POLL_INTERVAL,
        },
        daemon=True,
    )
    interactive.start()
    procs.append(interactive)
    return stop_event, procs


def stop_pool(handle) -> None:
    if not handle:
        return
    stop_event, procs = handle
    stop_event.set()
    for proc in procs:
        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate()
